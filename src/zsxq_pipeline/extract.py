"""Bounded adapter around the existing PDF text extractor.

The OCR and quality-gate implementation is deliberately kept in
``openclaw_tasks/zsxq_pdf_digest/extract_pdf_text.py`` for this migration.
This module gives the new pipeline a typed, subprocess-only boundary around
that implementation: the legacy script receives an isolated manifest copy,
the original manifest is replaced atomically only after its output validates,
and legacy error strings are converted into the state core's error categories.

It intentionally does *not* import the legacy extractor.  Importing it would
make process-global environment and OCR settings part of the Python worker and
would duplicate the exact OCR implementation that the migration is meant to
reuse unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import ErrorCategory


class ExtractionError(RuntimeError):
    """Base class for an extractor adapter failure."""


class ExtractionContractError(ExtractionError):
    """The packaged extractor or its manifest contract is unavailable."""


class ExtractionTransientError(ExtractionError):
    """The extractor exceeded a bounded runtime and may be retried."""


class ExtractionValidationError(ExtractionError):
    """The extractor returned a manifest outside the migration contract."""


_SHA256_HEX = frozenset("0123456789abcdef")
_LEGACY_ERROR_CATEGORY: dict[str, ErrorCategory] = {
    "content_failure": ErrorCategory.CONTENT,
    "env_failure": ErrorCategory.RELEASE_CONTRACT,
    "transient_failure": ErrorCategory.TRANSIENT,
    "auth_failure": ErrorCategory.AUTH,
    "authentication_failure": ErrorCategory.AUTH,
}


def _repo_root() -> Path:
    # ``src/zsxq_pipeline/extract.py`` -> repository root.
    return Path(__file__).resolve().parents[2]


def legacy_extractor_path() -> Path:
    """Return the source-controlled legacy extractor used during PR4.

    A caller may still pass an explicit path (notably release packaging and
    tests), but silently looking up a task directory in ``$HOME`` would revive
    the mutable OpenClaw deployment dependency that PR4 removes.
    """

    return _repo_root() / "openclaw_tasks" / "zsxq_pdf_digest" / "extract_pdf_text.py"


def sha256_file(path: str | Path) -> str:
    """Hash a PDF or extracted-text artifact without loading it all in memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha256(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    if len(normalized) == 64 and set(normalized) <= _SHA256_HEX:
        return normalized
    return None


def _required_text(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ExtractionValidationError(f"{field} is required")
    return text


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExtractionContractError(f"extractor did not produce manifest: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ExtractionValidationError(f"manifest is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ExtractionValidationError("batch manifest must be a JSON object")
    return payload


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write a JSON commit marker atomically, with a durable file payload."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        # The temporary file is owned by this invocation.  It may already have
        # been renamed, in which case unlinking is intentionally a no-op.
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def classify_extraction_failure(error_type: object, *, default: ErrorCategory = ErrorCategory.INVARIANT) -> ErrorCategory:
    """Translate the legacy extractor's stable failure families.

    The old shell worker called unavailable binaries and temporary-directory
    failures ``env_failure``.  In the state core those are release-contract
    blocks, not per-PDF retries.  Content failures remain quarantined and only
    genuine runtime interruptions are retryable.
    """

    return _LEGACY_ERROR_CATEGORY.get(str(error_type or "").strip().lower(), default)


@dataclass(frozen=True, slots=True)
class ExtractionItem:
    """Validated extraction outcome for one manifest file."""

    path: str
    filename: str
    pdf_sha256: str
    extractor_version: str
    status: str
    text_path: Path | None = None
    text_chars: int = 0
    text_source: str = ""
    cached: bool = False
    warning: str = ""
    diagnostics: Mapping[str, Any] | None = None
    error_category: ErrorCategory | None = None
    error_code: str = ""
    error_detail: str = ""
    retryable: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status == "success"


@dataclass(frozen=True, slots=True)
class ExtractionBatchResult:
    """The validated batch returned by :class:`ExtractorAdapter`."""

    manifest: Mapping[str, Any]
    items: tuple[ExtractionItem, ...]
    stdout: str
    stderr: str

    @property
    def successful(self) -> tuple[ExtractionItem, ...]:
        return tuple(item for item in self.items if item.succeeded)

    @property
    def failed(self) -> tuple[ExtractionItem, ...]:
        return tuple(item for item in self.items if not item.succeeded)


def _item_pdf_sha(item: Mapping[str, Any], path: str) -> str:
    direct = _valid_sha256(item.get("pdf_sha256"))
    if direct:
        return direct
    # The legacy extractor's cache key is exactly the PDF SHA-256.  Accept it
    # only when it has the full digest shape, never a user-selected cache name.
    cached = _valid_sha256(item.get("text_extract_cache_key"))
    if cached:
        return cached
    local_path = Path(path).expanduser()
    if local_path.is_file():
        return sha256_file(local_path)
    raise ExtractionValidationError(f"pdf_sha256 is required for {path}")


def validate_extracted_manifest(payload: Mapping[str, Any]) -> tuple[ExtractionItem, ...]:
    """Validate the legacy output while allowing individual content failures.

    A bad PDF must not invalidate already usable siblings.  Consequently a
    failed item is returned with a state error category instead of raising; a
    malformed manifest or a claimed-success artifact is a contract error and
    does raise.
    """

    raw_files = payload.get("files")
    if not isinstance(raw_files, list):
        raise ExtractionValidationError("batch manifest files must be a list")

    seen_paths: set[str] = set()
    items: list[ExtractionItem] = []
    for raw in raw_files:
        if not isinstance(raw, Mapping):
            raise ExtractionValidationError("every batch file must be an object")
        path = _required_text(raw.get("path"), field="file.path")
        if path in seen_paths:
            raise ExtractionValidationError(f"duplicate batch file path: {path}")
        seen_paths.add(path)
        filename = str(raw.get("filename") or Path(path).name).strip() or Path(path).name
        pdf_sha256 = _item_pdf_sha(raw, path)
        status = str(raw.get("text_extract_status", "")).strip().lower()
        if status == "success":
            text_path_text = _required_text(raw.get("extracted_text_path"), field=f"extracted_text_path for {path}")
            text_path = Path(text_path_text).expanduser().resolve(strict=False)
            if not text_path.is_file():
                raise ExtractionValidationError(f"extracted text is missing for {path}: {text_path}")
            text = text_path.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                raise ExtractionValidationError(f"extracted text is empty for {path}")
            declared_chars = int(raw.get("extracted_text_chars", 0) or 0)
            if declared_chars <= 0:
                raise ExtractionValidationError(f"extracted_text_chars must be positive for {path}")
            # The legacy extractor counts stripped text.  Detect impossible
            # claims but tolerate a trailing newline difference.
            if declared_chars != len(text):
                raise ExtractionValidationError(
                    f"extracted_text_chars mismatch for {path}: expected {len(text)}, got {declared_chars}"
                )
            extractor_version = _required_text(raw.get("text_extract_profile"), field=f"text_extract_profile for {path}")
            items.append(
                ExtractionItem(
                    path=path,
                    filename=filename,
                    pdf_sha256=pdf_sha256,
                    extractor_version=extractor_version,
                    status="success",
                    text_path=text_path,
                    text_chars=declared_chars,
                    text_source=_required_text(raw.get("text_source"), field=f"text_source for {path}"),
                    cached=bool(raw.get("text_extract_cached", False)),
                    warning=str(raw.get("text_extract_warning", "")).strip(),
                    diagnostics=dict(raw.get("text_extract_diagnostics", {}))
                    if isinstance(raw.get("text_extract_diagnostics", {}), Mapping)
                    else {},
                )
            )
            continue

        if status != "failed":
            raise ExtractionValidationError(f"text_extract_status must be success or failed for {path}")
        error_type = _required_text(raw.get("text_extract_error_type"), field=f"text_extract_error_type for {path}")
        error_detail = _required_text(raw.get("text_extract_error"), field=f"text_extract_error for {path}")
        items.append(
            ExtractionItem(
                path=path,
                filename=filename,
                pdf_sha256=pdf_sha256,
                extractor_version=str(raw.get("text_extract_profile", "")).strip(),
                status="failed",
                cached=bool(raw.get("text_extract_cached", False)),
                warning=str(raw.get("text_extract_warning", "")).strip(),
                diagnostics=dict(raw.get("text_extract_diagnostics", {}))
                if isinstance(raw.get("text_extract_diagnostics", {}), Mapping)
                else {},
                error_category=classify_extraction_failure(error_type),
                error_code=str(raw.get("text_extract_error_code", "")).strip() or "extract_failed",
                error_detail=error_detail,
                retryable=bool(raw.get("text_extract_retryable", False)),
            )
        )
    return tuple(items)


class ExtractorAdapter:
    """Run the retained extractor through a bounded argv-only subprocess."""

    def __init__(
        self,
        *,
        script_path: str | Path | None = None,
        python_executable: str | Path | None = None,
        timeout_seconds: int = 600,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if int(timeout_seconds) <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.script_path = Path(script_path) if script_path is not None else legacy_extractor_path()
        self.python_executable = str(python_executable or sys.executable)
        self.timeout_seconds = int(timeout_seconds)
        self.environment = dict(environment or {})

    def _check_script(self) -> Path:
        script = self.script_path.expanduser().resolve(strict=False)
        if not script.is_file():
            raise ExtractionContractError(f"legacy extractor is unavailable: {script}")
        return script

    def preflight(self) -> Mapping[str, Any]:
        """Run the legacy toolchain preflight without altering a batch."""

        script = self._check_script()
        environment = os.environ.copy()
        environment.update(self.environment)
        try:
            completed = subprocess.run(
                [self.python_executable, str(script), "--preflight-only"],
                cwd=str(script.parent),
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExtractionTransientError(f"extractor preflight timed out after {self.timeout_seconds}s") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise ExtractionContractError(f"extractor preflight failed: {detail[:512]}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ExtractionValidationError("extractor preflight did not emit JSON") from exc
        if not isinstance(payload, dict):
            raise ExtractionValidationError("extractor preflight must emit an object")
        if not bool(payload.get("ok", False)):
            raise ExtractionContractError("extractor preflight reports an unavailable toolchain")
        return payload

    def extract_batch(
        self,
        batch_file: str | Path,
        output_dir: str | Path,
        *,
        commit_manifest: bool = True,
    ) -> ExtractionBatchResult:
        """Extract a manifest through a staged copy and validate all outcomes.

        The legacy script mutates the manifest it receives.  Passing it a copy
        ensures a timeout, malformed output, or partial crash never exposes an
        unvalidated partial manifest to the new stateful pipeline.
        """

        script = self._check_script()
        original_path = Path(batch_file).expanduser().resolve(strict=True)
        original = _load_json_object(original_path)
        if not isinstance(original.get("files"), list):
            raise ExtractionValidationError("batch manifest files must be a list")

        destination = Path(output_dir).expanduser().resolve(strict=False)
        destination.mkdir(parents=True, exist_ok=True)
        staged_manifest = destination / f".extract-manifest-{uuid.uuid4().hex}.json"
        _atomic_write_json(staged_manifest, original)
        environment = os.environ.copy()
        environment.update(self.environment)
        argv = [
            self.python_executable,
            str(script),
            "--batch-file",
            str(staged_manifest),
            "--output-dir",
            str(destination),
        ]
        try:
            try:
                completed = subprocess.run(
                    argv,
                    cwd=str(script.parent),
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise ExtractionTransientError(f"extractor timed out after {self.timeout_seconds}s") from exc
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                raise ExtractionContractError(f"extractor process failed ({completed.returncode}): {detail[:512]}")
            updated = _load_json_object(staged_manifest)
            items = validate_extracted_manifest(updated)
            if commit_manifest:
                _atomic_write_json(original_path, updated)
            return ExtractionBatchResult(
                manifest=updated,
                items=items,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        finally:
            try:
                staged_manifest.unlink()
            except FileNotFoundError:
                pass


def record_extracted_text_artifact(
    state: Any,
    document_id: int,
    item: ExtractionItem,
    *,
    now: Any | None = None,
) -> Any:
    """Persist a successful text artifact through the state core's public API.

    Stage ownership stays in ``process.py``; this helper only makes the
    artifact identity and useful provenance explicit for that caller.
    """

    if not item.succeeded or item.text_path is None:
        raise ValueError("only successful extraction items can be recorded")
    return state.record_artifact(
        document_id,
        kind="extracted_text",
        path=item.text_path,
        pdf_sha256=item.pdf_sha256,
        content_sha256=sha256_file(item.text_path),
        extractor_version=item.extractor_version,
        size_bytes=item.text_path.stat().st_size,
        metadata={
            "text_source": item.text_source,
            "text_chars": item.text_chars,
            "cached": item.cached,
            "warning": item.warning,
            "diagnostics": dict(item.diagnostics or {}),
        },
        now=now,
    )


__all__ = [
    "ExtractionBatchResult",
    "ExtractionContractError",
    "ExtractionError",
    "ExtractionItem",
    "ExtractionTransientError",
    "ExtractionValidationError",
    "ExtractorAdapter",
    "classify_extraction_failure",
    "legacy_extractor_path",
    "record_extracted_text_artifact",
    "sha256_file",
    "validate_extracted_manifest",
]
