"""Deterministic summary identities, artifacts, validation, and job primitives.

This module owns no model subprocess.  A provider receives an already bounded
set of extracted-text inputs and returns schema JSON; this module verifies that
JSON against the manifest, persists a durable local artifact, and exposes a
small two-worker scheduler primitive.  Keeping those boundaries separate is
what lets a failed publish retry independently of extraction or summarization.
"""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar

from .model import SummaryIdentity


SUMMARY_ARTIFACT_SCHEMA_VERSION = 1
_SHA256_HEX = frozenset("0123456789abcdef")
_ROOT_FIELDS = frozenset({"status", "handled_count", "handled_paths", "summaries", "error"})
_ENTRY_FIELDS = frozenset({"path", "filename", "title", "quality_hint", "markdown"})


class SummaryError(RuntimeError):
    """Base class for summary-contract and artifact failures."""


class SummaryValidationError(SummaryError):
    """Model output does not satisfy the fixed summary schema."""


class SummaryModelFailure(SummaryError):
    """The model reported a valid terminal failure payload."""


class SummaryInvariantError(SummaryError):
    """One cache identity attempted to produce incompatible durable content."""


class SummaryCacheCorruptionError(SummaryError):
    """A cache commit marker exists but its paired artifact is invalid."""


def _valid_sha256(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text if len(text) == 64 and set(text) <= _SHA256_HEX else None


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise SummaryValidationError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise SummaryValidationError(f"{field} must not be empty")
    return text


def _canonical_path(value: object, *, field: str) -> str:
    text = _required_text(value, field=field)
    # Do not collapse internal whitespace or rewrite separators.  The provider
    # receives exactly these strings and its schema-level validator compares
    # them byte-for-byte after outer whitespace trimming.  NFC only avoids an
    # accidental Unicode representation distinction in a local manifest.
    return unicodedata.normalize("NFC", text)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _atomic_write_text(path: Path, value: str) -> None:
    """Atomically replace a text artifact after syncing its temporary bytes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def prompt_version_hash(prompt: str, system_prompt: str = "") -> str:
    """Hash the exact prompt bytes that influence a model completion."""

    if not isinstance(prompt, str) or not isinstance(system_prompt, str):
        raise TypeError("prompt and system_prompt must be strings")
    payload = json.dumps(
        {"prompt": prompt, "system_prompt": system_prompt},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_text(payload)


@dataclass(frozen=True, slots=True)
class SummaryInput:
    """One verified extracted-text input made available to a provider."""

    path: str
    filename: str
    text: str
    title: str = ""
    report_id: str = ""
    text_source: str = ""


@dataclass(frozen=True, slots=True)
class SummaryEntry:
    """The schema-valid human-readable output for one source PDF."""

    path: str
    filename: str
    title: str
    quality_hint: str
    markdown: str

    def to_payload(self) -> dict[str, str]:
        return {
            "path": self.path,
            "filename": self.filename,
            "title": self.title,
            "quality_hint": self.quality_hint,
            "markdown": self.markdown,
        }


@dataclass(frozen=True, slots=True)
class SummaryResult:
    """A normalized result from the fixed provider schema."""

    status: str
    handled_count: int
    handled_paths: tuple[str, ...]
    summaries: tuple[SummaryEntry, ...]
    error: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == "success"


@dataclass(frozen=True, slots=True)
class SummaryArtifactPaths:
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True, slots=True)
class PersistedSummary:
    identity: SummaryIdentity
    entry: SummaryEntry
    paths: SummaryArtifactPaths
    markdown_sha256: str
    source_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SummaryBatchArtifact:
    """Locally committed summary material ready for deterministic publication."""

    result: SummaryResult
    entries: tuple[PersistedSummary, ...]
    output_json: Path
    output_markdown: Path
    summary_sha256: str


def manifest_expected_paths(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Read ordered paths from a batch manifest without accepting duplicates."""

    files = manifest.get("files")
    if not isinstance(files, list):
        raise SummaryValidationError("manifest.files must be a list")
    paths: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(files):
        if not isinstance(item, Mapping):
            raise SummaryValidationError(f"manifest.files[{index}] must be an object")
        path = _canonical_path(item.get("path"), field=f"manifest.files[{index}].path")
        if path in seen:
            raise SummaryValidationError(f"manifest has duplicate path: {path}")
        seen.add(path)
        paths.append(path)
    return tuple(paths)


def build_summary_inputs(manifest: Mapping[str, Any]) -> tuple[SummaryInput, ...]:
    """Load only extracted text, never PDFs, for a model job."""

    files = manifest.get("files")
    expected = manifest_expected_paths(manifest)
    assert isinstance(files, list)  # narrowed by manifest_expected_paths
    inputs: list[SummaryInput] = []
    for index, (raw, path) in enumerate(zip(files, expected, strict=True)):
        assert isinstance(raw, Mapping)
        text_path_text = _required_text(raw.get("extracted_text_path"), field=f"files[{index}].extracted_text_path")
        text_path = Path(text_path_text).expanduser()
        if not text_path.is_file():
            raise SummaryValidationError(f"extracted text is missing for {path}: {text_path}")
        text = text_path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            raise SummaryValidationError(f"extracted text is empty for {path}")
        declared_chars = raw.get("extracted_text_chars")
        if declared_chars is not None and int(declared_chars or 0) != len(text):
            raise SummaryValidationError(f"extracted_text_chars mismatch for {path}")
        filename = str(raw.get("filename") or Path(path).name).strip() or Path(path).name
        inputs.append(
            SummaryInput(
                path=path,
                filename=filename,
                text=text,
                title=str(raw.get("title", "")).strip(),
                report_id=str(raw.get("report_id", "")).strip(),
                text_source=str(raw.get("text_source", "")).strip(),
            )
        )
    return tuple(inputs)


def summary_identity_for_item(
    item: Mapping[str, Any],
    *,
    prompt_version: str,
    model: str,
    reasoning: str,
) -> SummaryIdentity:
    """Build the full durable identity from an extracted manifest item."""

    pdf_sha256 = _valid_sha256(item.get("pdf_sha256")) or _valid_sha256(item.get("text_extract_cache_key"))
    if pdf_sha256 is None:
        raise SummaryValidationError("summary identity requires item.pdf_sha256")
    extractor_version = str(item.get("text_extract_profile") or item.get("extractor_version") or "").strip()
    if not extractor_version:
        raise SummaryValidationError("summary identity requires text_extract_profile")
    try:
        return SummaryIdentity(
            pdf_sha256=pdf_sha256,
            extractor_version=extractor_version,
            prompt_version=str(prompt_version).strip(),
            model=str(model).strip(),
            reasoning=str(reasoning).strip(),
        )
    except ValueError as exc:
        raise SummaryValidationError(str(exc)) from exc


def identities_for_manifest(
    manifest: Mapping[str, Any],
    *,
    prompt_version: str,
    model: str,
    reasoning: str,
) -> dict[str, SummaryIdentity]:
    files = manifest.get("files")
    paths = manifest_expected_paths(manifest)
    assert isinstance(files, list)
    return {
        path: summary_identity_for_item(item, prompt_version=prompt_version, model=model, reasoning=reasoning)
        for path, item in zip(paths, files, strict=True)
        if isinstance(item, Mapping)
    }


def _validate_entry(raw: object, *, expected_path: str) -> SummaryEntry:
    if not isinstance(raw, Mapping):
        raise SummaryValidationError("each summary must be an object")
    keys = frozenset(raw.keys())
    unknown = sorted(str(key) for key in keys - _ENTRY_FIELDS)
    if unknown:
        raise SummaryValidationError(f"summary has unsupported field(s): {', '.join(unknown)}")
    required = {"path", "filename", "title", "markdown"}
    missing = sorted(required - keys)
    if missing:
        raise SummaryValidationError(f"summary is missing required field(s): {', '.join(missing)}")
    path = _canonical_path(raw.get("path"), field="summary.path")
    if path != expected_path:
        raise SummaryValidationError(f"summary path does not match manifest order: {path}")
    quality_hint = raw.get("quality_hint", "")
    if not isinstance(quality_hint, str):
        raise SummaryValidationError("summary.quality_hint must be a string")
    return SummaryEntry(
        path=path,
        filename=_required_text(raw.get("filename"), field="summary.filename"),
        title=_required_text(raw.get("title"), field="summary.title"),
        quality_hint=quality_hint.strip(),
        markdown=_required_text(raw.get("markdown"), field="summary.markdown"),
    )


def validate_summary_payload(payload: Mapping[str, Any], *, expected_paths: Sequence[str]) -> SummaryResult:
    """Enforce the provider schema and the batch's exact ordered path contract.

    The provider's JSON-schema flag constrains generation, but a local check is
    still mandatory: the output schema cannot know which manifest this specific
    invocation was allowed to handle.
    """

    # Keep the model provider's JSON-schema-adjacent validator as the single
    # source for wire-level rules when it is installed.  The local checks below
    # add typed result construction and remain useful for an intentionally
    # minimal package import during staged development.
    try:
        from .providers.codex import SummaryOutputValidationError, validate_summary_payload as provider_validate
    except ImportError:  # pragma: no cover - only useful while assembling a partial package
        provider_validate = None
    else:
        try:
            provider_validate(payload, expected_paths=expected_paths)
        except SummaryOutputValidationError as exc:
            raise SummaryValidationError(str(exc)) from exc

    if not isinstance(payload, Mapping):
        raise SummaryValidationError("summary result must be an object")
    keys = frozenset(payload.keys())
    unknown = sorted(str(key) for key in keys - _ROOT_FIELDS)
    if unknown:
        raise SummaryValidationError(f"summary result has unsupported field(s): {', '.join(unknown)}")
    required = {"status", "handled_count", "handled_paths", "summaries"}
    missing = sorted(required - keys)
    if missing:
        raise SummaryValidationError(f"summary result is missing required field(s): {', '.join(missing)}")

    status = payload.get("status")
    if status not in {"success", "failed"}:
        raise SummaryValidationError("summary status must be success or failed")
    handled_count = payload.get("handled_count")
    if isinstance(handled_count, bool) or not isinstance(handled_count, int):
        raise SummaryValidationError("handled_count must be an integer")
    raw_paths = payload.get("handled_paths")
    if not isinstance(raw_paths, list) or not all(isinstance(path, str) for path in raw_paths):
        raise SummaryValidationError("handled_paths must be a string list")
    raw_summaries = payload.get("summaries")
    if not isinstance(raw_summaries, list):
        raise SummaryValidationError("summaries must be a list")

    normalized_expected = tuple(_canonical_path(path, field="expected path") for path in expected_paths)
    if len(set(normalized_expected)) != len(normalized_expected):
        raise SummaryValidationError("expected_paths must not contain duplicates")
    normalized_handled = tuple(_canonical_path(path, field="handled path") for path in raw_paths)

    if status == "failed":
        if "error" not in keys:
            raise SummaryValidationError("failed summary result requires error")
        error = _required_text(payload.get("error"), field="error")
        if handled_count != 0 or normalized_handled or raw_summaries:
            raise SummaryValidationError("failed summary result must handle no paths and contain no summaries")
        return SummaryResult("failed", 0, (), (), error)

    if "error" in keys:
        raise SummaryValidationError("successful summary result must not contain error")
    if handled_count != len(normalized_expected):
        raise SummaryValidationError(
            f"handled_count mismatch: expected {len(normalized_expected)}, got {handled_count}"
        )
    if normalized_handled != normalized_expected:
        raise SummaryValidationError("handled_paths must exactly match manifest paths in order")
    if len(raw_summaries) != len(normalized_expected):
        raise SummaryValidationError(
            f"summary entry count mismatch: expected {len(normalized_expected)}, got {len(raw_summaries)}"
        )
    entries = tuple(
        _validate_entry(entry, expected_path=expected_path)
        for entry, expected_path in zip(raw_summaries, normalized_expected, strict=True)
    )
    return SummaryResult("success", handled_count, normalized_handled, entries)


def validate_provider_result(result: Any, *, expected_paths: Sequence[str]) -> SummaryResult:
    """Validate either a provider result object or its direct mapping payload."""

    payload = getattr(result, "output", result)
    if not isinstance(payload, Mapping):
        raise SummaryValidationError("provider result has no mapping output")
    return validate_summary_payload(payload, expected_paths=expected_paths)


class SummaryStore:
    """Filesystem cache where JSON acts as the commit marker for Markdown."""

    def __init__(self, cache_root: str | Path) -> None:
        self.cache_root = Path(cache_root).expanduser().resolve(strict=False)

    def paths(self, identity: SummaryIdentity) -> SummaryArtifactPaths:
        return SummaryArtifactPaths(
            json_path=self.cache_root / "json" / f"{identity.cache_key}.json",
            markdown_path=self.cache_root / "markdown" / f"{identity.cache_key}.md",
        )

    @staticmethod
    def _identity_payload(identity: SummaryIdentity) -> dict[str, Any]:
        decoded = json.loads(identity.canonical_json)
        assert isinstance(decoded, dict)
        return decoded

    def load(self, identity: SummaryIdentity, *, expected_path: str | None = None) -> PersistedSummary | None:
        """Return a complete cache hit; reject partially committed/corrupt hits."""

        paths = self.paths(identity)
        if not paths.json_path.exists():
            return None
        try:
            payload = json.loads(paths.json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SummaryCacheCorruptionError(f"summary cache JSON is unreadable: {paths.json_path}") from exc
        if not isinstance(payload, Mapping):
            raise SummaryCacheCorruptionError("summary cache JSON must be an object")
        if payload.get("schema_version") != SUMMARY_ARTIFACT_SCHEMA_VERSION:
            raise SummaryCacheCorruptionError("summary cache schema version mismatch")
        if payload.get("cache_key") != identity.cache_key:
            raise SummaryCacheCorruptionError("summary cache key mismatch")
        if payload.get("identity") != self._identity_payload(identity):
            raise SummaryCacheCorruptionError("summary cache identity mismatch")
        markdown_path = Path(str(payload.get("markdown_path", ""))).expanduser().resolve(strict=False)
        if markdown_path != paths.markdown_path:
            raise SummaryCacheCorruptionError("summary cache markdown path mismatch")
        if not markdown_path.is_file():
            raise SummaryCacheCorruptionError("summary cache markdown is missing")
        markdown = markdown_path.read_text(encoding="utf-8", errors="replace").strip()
        if not markdown:
            raise SummaryCacheCorruptionError("summary cache markdown is empty")
        if payload.get("markdown_sha256") != _sha256_text(markdown):
            raise SummaryCacheCorruptionError("summary cache markdown checksum mismatch")
        raw_entry = payload.get("entry")
        if not isinstance(raw_entry, Mapping):
            raise SummaryCacheCorruptionError("summary cache entry is missing")
        path = _canonical_path(raw_entry.get("path"), field="cache entry path")
        if expected_path is not None and path != _canonical_path(expected_path, field="expected path"):
            return None
        try:
            entry = _validate_entry(raw_entry, expected_path=path)
        except SummaryValidationError as exc:
            raise SummaryCacheCorruptionError("summary cache entry is invalid") from exc
        if entry.markdown != markdown:
            raise SummaryCacheCorruptionError("summary cache JSON and markdown differ")
        source_metadata = payload.get("source_metadata", {})
        if not isinstance(source_metadata, Mapping):
            raise SummaryCacheCorruptionError("summary cache source_metadata must be an object")
        return PersistedSummary(
            identity=identity,
            entry=entry,
            paths=paths,
            markdown_sha256=_sha256_text(markdown),
            source_metadata=dict(source_metadata),
        )

    def persist(
        self,
        identity: SummaryIdentity,
        entry: SummaryEntry,
        *,
        source_metadata: Mapping[str, Any] | None = None,
    ) -> PersistedSummary:
        """Atomically persist one Markdown/JSON pair, never silently rebind it."""

        normalized_entry = _validate_entry(entry.to_payload(), expected_path=entry.path)
        # Look up by the durable identity before considering the source path.
        # Multiple source records may legitimately reference the same PDF
        # bytes; a different local path must never overwrite that cache entry.
        existing = self.load(identity)
        if existing is not None:
            if (
                existing.entry.markdown != normalized_entry.markdown
                or existing.entry.title != normalized_entry.title
                or existing.entry.quality_hint != normalized_entry.quality_hint
            ):
                raise SummaryInvariantError("one summary identity produced conflicting durable content")
            # Filename/path are source presentation metadata, not model
            # identity.  Return the caller's validated path for deterministic
            # publication while retaining the immutable cache artifact.
            return PersistedSummary(
                identity=identity,
                entry=normalized_entry,
                paths=existing.paths,
                markdown_sha256=existing.markdown_sha256,
                source_metadata=dict(source_metadata or existing.source_metadata),
            )

        paths = self.paths(identity)
        markdown = normalized_entry.markdown
        checksum = _sha256_text(markdown)
        metadata = dict(source_metadata or {})
        # Markdown goes first.  Its JSON companion is the commit marker: a
        # crash between writes is a cache miss, never an accepted stale hit.
        _atomic_write_text(paths.markdown_path, markdown + "\n")
        _atomic_write_json(
            paths.json_path,
            {
                "schema_version": SUMMARY_ARTIFACT_SCHEMA_VERSION,
                "cache_key": identity.cache_key,
                "identity": self._identity_payload(identity),
                "entry": normalized_entry.to_payload(),
                "markdown_path": str(paths.markdown_path),
                "markdown_sha256": checksum,
                "source_metadata": metadata,
            },
        )
        return PersistedSummary(
            identity=identity,
            entry=normalized_entry,
            paths=paths,
            markdown_sha256=checksum,
            source_metadata=metadata,
        )


def _source_metadata_for_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "extracted_text_path": str(item.get("extracted_text_path", "")).strip(),
        "extracted_text_chars": int(item.get("extracted_text_chars", 0) or 0),
        "text_source": str(item.get("text_source", "")).strip(),
        "text_extract_warning": str(item.get("text_extract_warning", "")).strip(),
    }


def _write_batch_artifacts(
    *,
    result: SummaryResult,
    entries: Sequence[PersistedSummary],
    output_json: Path,
    output_markdown: Path,
    manifest: Mapping[str, Any],
) -> SummaryBatchArtifact:
    if not result.succeeded:
        raise SummaryModelFailure(result.error or "summary provider reported failure")
    markdown = "\n\n".join(entry.entry.markdown for entry in entries).strip()
    if not markdown and entries:
        raise SummaryInvariantError("successful summary batch has no Markdown")
    payload = {
        "schema_version": SUMMARY_ARTIFACT_SCHEMA_VERSION,
        "handled_count": result.handled_count,
        "handled_paths": list(result.handled_paths),
        "chunk_index": int(manifest.get("chunk_index", 1) or 1),
        "chunk_total": int(manifest.get("chunk_total", 1) or 1),
        "entries": [
            {
                "path": persisted.entry.path,
                "filename": persisted.entry.filename,
                "title": persisted.entry.title,
                "quality_hint": persisted.entry.quality_hint,
                "summary_identity": json.loads(persisted.identity.canonical_json),
                "cache_key": persisted.identity.cache_key,
                "markdown_path": str(persisted.paths.markdown_path),
                "json_path": str(persisted.paths.json_path),
                "markdown_sha256": persisted.markdown_sha256,
            }
            for persisted in entries
        ],
    }
    # As with cache artifacts, the JSON is the commit marker for the batch.
    _atomic_write_text(output_markdown, markdown + ("\n" if markdown else ""))
    _atomic_write_json(output_json, payload)
    return SummaryBatchArtifact(
        result=result,
        entries=tuple(entries),
        output_json=output_json,
        output_markdown=output_markdown,
        summary_sha256=_sha256_text(markdown),
    )


def persist_summary_batch(
    manifest: Mapping[str, Any],
    provider_result: Any,
    *,
    identities: Mapping[str, SummaryIdentity],
    store: SummaryStore,
    output_json: str | Path,
    output_markdown: str | Path,
) -> SummaryBatchArtifact:
    """Validate then persist a provider output in manifest order.

    Cache writes happen before the batch commit marker.  The stateful process
    should record the returned Markdown artifacts and complete stage leases
    only after this function returns.
    """

    paths = manifest_expected_paths(manifest)
    result = validate_provider_result(provider_result, expected_paths=paths)
    if not result.succeeded:
        raise SummaryModelFailure(result.error)
    files = manifest.get("files")
    assert isinstance(files, list)
    persisted_entries: list[PersistedSummary] = []
    for item, entry in zip(files, result.summaries, strict=True):
        assert isinstance(item, Mapping)
        identity = identities.get(entry.path)
        if identity is None:
            raise SummaryValidationError(f"no summary identity for {entry.path}")
        persisted_entries.append(store.persist(identity, entry, source_metadata=_source_metadata_for_item(item)))
    return _write_batch_artifacts(
        result=result,
        entries=persisted_entries,
        output_json=Path(output_json).expanduser().resolve(strict=False),
        output_markdown=Path(output_markdown).expanduser().resolve(strict=False),
        manifest=manifest,
    )


def materialize_summary_cache(
    manifest: Mapping[str, Any],
    *,
    identities: Mapping[str, SummaryIdentity],
    store: SummaryStore,
    output_json: str | Path,
    output_markdown: str | Path,
) -> SummaryBatchArtifact | None:
    """Materialize a complete cache hit; return ``None`` if any entry misses."""

    paths = manifest_expected_paths(manifest)
    files = manifest.get("files")
    assert isinstance(files, list)
    entries: list[PersistedSummary] = []
    for item, path in zip(files, paths, strict=True):
        assert isinstance(item, Mapping)
        identity = identities.get(path)
        if identity is None:
            raise SummaryValidationError(f"no summary identity for {path}")
        cached = store.load(identity, expected_path=path)
        if cached is None:
            return None
        entries.append(cached)
    result = SummaryResult(
        status="success",
        handled_count=len(paths),
        handled_paths=paths,
        summaries=tuple(entry.entry for entry in entries),
    )
    return _write_batch_artifacts(
        result=result,
        entries=entries,
        output_json=Path(output_json).expanduser().resolve(strict=False),
        output_markdown=Path(output_markdown).expanduser().resolve(strict=False),
        manifest=manifest,
    )


def record_summary_artifact(
    state: Any,
    document_id: int,
    persisted: PersistedSummary,
    *,
    now: Any | None = None,
) -> Any:
    """Record a cache-backed summary through the public state API.

    ``reasoning`` is forwarded explicitly, so a state lookup cannot merge two
    model calls that share every older identity component but use a different
    reasoning level.
    """

    return state.record_artifact(
        document_id,
        kind="summary",
        path=persisted.paths.markdown_path,
        pdf_sha256=persisted.identity.pdf_sha256,
        content_sha256=persisted.markdown_sha256,
        extractor_version=persisted.identity.extractor_version,
        prompt_version=persisted.identity.prompt_version,
        model=persisted.identity.model,
        reasoning=persisted.identity.reasoning,
        size_bytes=persisted.paths.markdown_path.stat().st_size,
        metadata={
            "cache_key": persisted.identity.cache_key,
            "source_metadata": dict(persisted.source_metadata),
            "entry_path": persisted.entry.path,
            "title": persisted.entry.title,
        },
        now=now,
    )


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class SummaryJob:
    """Provider-neutral unit of work; callers keep publication ordering here."""

    job_id: str
    expected_paths: tuple[str, ...]
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SummaryJobOutcome(Generic[T]):
    job: SummaryJob
    result: T | None = None
    error: Exception | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


def run_summary_jobs(
    jobs: Iterable[SummaryJob],
    run_job: Callable[[SummaryJob], T],
    *,
    max_workers: int = 2,
) -> tuple[SummaryJobOutcome[T], ...]:
    """Run at most two summaries concurrently and return outcomes in input order.

    Completion order is intentionally discarded.  Publication uses the caller's
    deterministic manifest/group order and must never be reordered merely
    because one model invocation finished first.
    """

    if not 1 <= int(max_workers) <= 2:
        raise ValueError("max_workers must be between 1 and 2")
    ordered_jobs = tuple(jobs)
    if not ordered_jobs:
        return ()
    duplicate_ids = [job.job_id for job in ordered_jobs]
    if len(set(duplicate_ids)) != len(duplicate_ids):
        raise ValueError("summary job_id values must be unique")

    outcomes: list[SummaryJobOutcome[T] | None] = [None] * len(ordered_jobs)
    with ThreadPoolExecutor(max_workers=int(max_workers), thread_name_prefix="zsxq-summary") as pool:
        futures: dict[Future[T], tuple[int, SummaryJob]] = {
            pool.submit(run_job, job): (index, job) for index, job in enumerate(ordered_jobs)
        }
        for future in as_completed(futures):
            index, job = futures[future]
            try:
                outcomes[index] = SummaryJobOutcome(job=job, result=future.result())
            except Exception as exc:  # individual jobs are independently retried by the state layer
                outcomes[index] = SummaryJobOutcome(job=job, error=exc)
    return tuple(outcome for outcome in outcomes if outcome is not None)


__all__ = [
    "PersistedSummary",
    "SUMMARY_ARTIFACT_SCHEMA_VERSION",
    "SummaryArtifactPaths",
    "SummaryBatchArtifact",
    "SummaryCacheCorruptionError",
    "SummaryEntry",
    "SummaryError",
    "SummaryInput",
    "SummaryInvariantError",
    "SummaryJob",
    "SummaryJobOutcome",
    "SummaryModelFailure",
    "SummaryResult",
    "SummaryStore",
    "SummaryValidationError",
    "build_summary_inputs",
    "identities_for_manifest",
    "manifest_expected_paths",
    "materialize_summary_cache",
    "persist_summary_batch",
    "prompt_version_hash",
    "record_summary_artifact",
    "run_summary_jobs",
    "summary_identity_for_item",
    "validate_provider_result",
    "validate_summary_payload",
]
