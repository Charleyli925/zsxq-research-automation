"""Fail-closed adapter for one isolated ``codex exec`` summary invocation.

The summary worker intentionally gives Codex a newly materialized directory
instead of a repository checkout or a source PDF directory.  The only result
accepted from the CLI is its ``--output-last-message`` JSON file; stdout is
diagnostic/usage data and never becomes a summary artifact.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol


class CodexProviderError(RuntimeError):
    """Base error for the direct Codex provider."""


class CodexExecutionError(CodexProviderError):
    """The Codex executable could not be started or exited unsuccessfully."""


class CodexTimeoutError(CodexProviderError):
    """The isolated process group exceeded the configured hard deadline."""


class SummaryOutputValidationError(CodexProviderError):
    """The final model result was not the exact summary contract."""

    def __init__(self, errors: str | Sequence[str]) -> None:
        self.errors = (errors,) if isinstance(errors, str) else tuple(errors)
        super().__init__("invalid Codex summary output: " + "; ".join(self.errors))


@dataclass(frozen=True, slots=True)
class CodexProviderConfig:
    """Configuration that is safe to construct without pipeline TOML imports."""

    model: str
    work_root: Path | str
    command: str = "codex"
    reasoning: str = "medium"
    timeout_seconds: float = 900.0
    terminate_grace_seconds: float = 10.0
    retain_workdir: bool = False

    def __post_init__(self) -> None:
        if not str(self.command).strip():
            raise ValueError("Codex command is required")
        if not str(self.model).strip():
            raise ValueError("Codex model is required")
        if not str(self.reasoning).strip():
            raise ValueError("Codex reasoning is required")
        if self.timeout_seconds <= 0:
            raise ValueError("Codex timeout_seconds must be positive")
        if self.terminate_grace_seconds < 0:
            raise ValueError("Codex terminate_grace_seconds must be non-negative")
        root = Path(self.work_root).expanduser()
        if not root.is_absolute():
            raise ValueError("Codex work_root must be an absolute runtime path")
        object.__setattr__(self, "work_root", root.resolve(strict=False))
        object.__setattr__(self, "command", str(self.command).strip())
        object.__setattr__(self, "model", str(self.model).strip())
        object.__setattr__(self, "reasoning", str(self.reasoning).strip())


@dataclass(frozen=True, slots=True)
class CodexSummaryInput:
    """One already-extracted text input, identified by its durable source path."""

    path: str
    filename: str
    text: str

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise ValueError("summary input path is required")
        if not self.filename.strip():
            raise ValueError("summary input filename is required")


@dataclass(frozen=True, slots=True)
class CodexSummaryRequest:
    """All data that may enter one isolated Codex summary job.

    ``manifest`` is copied into the temporary work directory as JSON.  The
    original extracted files are never exposed by path: their text is copied
    into controlled files whose relative paths are supplied in the prompt.
    """

    job_id: str
    manifest: Mapping[str, Any]
    inputs: tuple[CodexSummaryInput, ...]
    prompt: str
    system_prompt: str = ""
    expected_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.job_id).strip():
            raise ValueError("summary job_id is required")
        if not isinstance(self.manifest, Mapping):
            raise ValueError("summary manifest must be an object")
        inputs = tuple(self.inputs)
        if not inputs:
            raise ValueError("a Codex summary job requires at least one input")
        if any(not isinstance(item, CodexSummaryInput) for item in inputs):
            raise TypeError("summary inputs must be CodexSummaryInput values")
        input_paths = tuple(item.path for item in inputs)
        if len(set(input_paths)) != len(input_paths):
            raise ValueError("summary input paths must be unique")
        expected = tuple(str(path).strip() for path in self.expected_paths) or input_paths
        if any(not path for path in expected):
            raise ValueError("expected summary paths must be non-empty")
        if len(set(expected)) != len(expected):
            raise ValueError("expected summary paths must be unique")
        if expected != input_paths:
            raise ValueError("expected summary paths must exactly match controlled inputs in order")
        if not str(self.prompt).strip():
            raise ValueError("summary prompt is required")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "expected_paths", expected)


@dataclass(frozen=True, slots=True)
class CodexCommandResult:
    """Small command result used by capability preflight injection."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True, slots=True)
class CodexSummaryResult:
    """Validated final output plus intentionally limited diagnostics."""

    output: Mapping[str, Any]
    usage: Mapping[str, Any]
    workdir: Path | None
    command: tuple[str, ...]
    diagnostics: str


class _Process(Protocol):
    pid: int
    returncode: int | None

    def communicate(self, input: str | None = None, timeout: float | None = None) -> tuple[str, str]: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


PopenFactory = Callable[..., _Process]
HelpExecutor = Callable[[tuple[str, ...]], CodexCommandResult]


_ROOT_FIELDS = frozenset({"status", "handled_count", "handled_paths", "summaries", "error"})
_SUMMARY_FIELDS = frozenset({"path", "filename", "title", "quality_hint", "markdown"})


def _path_identity(value: str) -> str:
    """Compare model echoes without changing the trusted filesystem path."""

    return unicodedata.normalize("NFC", str(value).strip())


def summary_schema_path() -> Path:
    """Return the installed fixed schema used for every direct summary call."""

    return Path(__file__).resolve().parents[1] / "schemas" / "summary.schema.json"


def summary_prompt_path() -> Path:
    """Return the migrated task prompt asset."""

    return Path(__file__).resolve().parents[1] / "prompts" / "summary.md"


def summary_system_prompt_path() -> Path:
    """Return the migrated system prompt asset."""

    return Path(__file__).resolve().parents[1] / "prompts" / "summary-system.md"


def summary_prompt_version() -> str:
    """Stable content hash for the two prompt assets used in cache identity."""

    digest = sha256()
    for path in (summary_system_prompt_path(), summary_prompt_path()):
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def validate_summary_payload(payload: Any, *, expected_paths: Sequence[str]) -> dict[str, Any]:
    """Validate the schema *and* the per-job manifest identity contract.

    JSON Schema can reject unknown keys but cannot express equality with this
    run's manifest.  Keeping that semantic check here makes an output valid
    only when it handled precisely the controlled inputs of this job.
    """

    errors: list[str] = []
    expected = tuple(str(path).strip() for path in expected_paths)
    expected_identities = tuple(_path_identity(path) for path in expected)
    if not expected or any(not path for path in expected):
        errors.append("expected_paths must contain one or more non-empty paths")
    elif len(set(expected_identities)) != len(expected_identities):
        errors.append("expected_paths must be unique")

    if not isinstance(payload, Mapping):
        raise SummaryOutputValidationError("root must be a JSON object")
    value = dict(payload)
    unknown = sorted(set(value) - _ROOT_FIELDS)
    if unknown:
        errors.append("root contains unsupported field(s): " + ", ".join(unknown))
    required = ("status", "handled_count", "handled_paths", "summaries", "error")
    missing = [field for field in required if field not in value]
    if missing:
        errors.append("root is missing required field(s): " + ", ".join(missing))

    status = value.get("status")
    if status not in {"success", "failed"}:
        errors.append("status must be success or failed")

    handled_count = value.get("handled_count")
    if not _is_nonnegative_int(handled_count):
        errors.append("handled_count must be a non-negative integer")

    handled_paths = value.get("handled_paths")
    if not isinstance(handled_paths, list) or any(not isinstance(path, str) or not path for path in handled_paths):
        errors.append("handled_paths must be an array of non-empty strings")
        handled_paths = []
    elif len({_path_identity(path) for path in handled_paths}) != len(handled_paths):
        errors.append("handled_paths must not contain duplicates")

    summaries = value.get("summaries")
    validated_summary_paths: list[str] = []
    if not isinstance(summaries, list):
        errors.append("summaries must be an array")
        summaries = []
    else:
        for index, summary in enumerate(summaries):
            if not isinstance(summary, Mapping):
                errors.append(f"summaries[{index}] must be an object")
                continue
            summary_value = dict(summary)
            summary_unknown = sorted(set(summary_value) - _SUMMARY_FIELDS)
            if summary_unknown:
                errors.append(
                    f"summaries[{index}] contains unsupported field(s): " + ", ".join(summary_unknown)
                )
            summary_missing = [
                field for field in ("path", "filename", "title", "quality_hint", "markdown") if field not in summary_value
            ]
            if summary_missing:
                errors.append(f"summaries[{index}] is missing required field(s): " + ", ".join(summary_missing))
            for field in ("path", "filename", "title", "markdown"):
                candidate = summary_value.get(field)
                if not isinstance(candidate, str) or not candidate:
                    errors.append(f"summaries[{index}].{field} must be a non-empty string")
            if not isinstance(summary_value.get("quality_hint"), str):
                errors.append(f"summaries[{index}].quality_hint must be a string")
            candidate_path = summary_value.get("path")
            if isinstance(candidate_path, str) and candidate_path:
                validated_summary_paths.append(candidate_path)

    if status == "success":
        if value.get("error") is not None:
            errors.append("success output requires error to be null")
        if [_path_identity(path) for path in handled_paths] != list(expected_identities):
            errors.append("success handled_paths must exactly equal the job manifest paths in order")
        if [_path_identity(path) for path in validated_summary_paths] != list(expected_identities):
            errors.append("success summary paths must exactly equal the job manifest paths in order")
        if handled_count != len(expected):
            errors.append("success handled_count must equal the manifest path count")
        if len(summaries) != len(expected):
            errors.append("success summaries length must equal the manifest path count")
    elif status == "failed":
        error = value.get("error")
        if not isinstance(error, str) or not error.strip():
            errors.append("failed output requires a non-empty error")
        if handled_count != 0:
            errors.append("failed handled_count must be 0")
        if handled_paths != []:
            errors.append("failed handled_paths must be empty")
        if summaries != []:
            errors.append("failed summaries must be empty")

    if errors:
        raise SummaryOutputValidationError(errors)
    return value


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _bind_single_input_identity(payload: Any, request: CodexSummaryRequest) -> Any:
    """Bind a structurally present single-result identity to controlled input.

    A one-input job has no model-side identity decision to make.  The runtime
    already owns its logical path and filename, so accepting a non-empty echo
    and replacing it with those trusted values avoids brittle Unicode/path
    transcription without relaxing multi-input ordering or schema checks.
    """

    if len(request.inputs) != 1 or len(request.expected_paths) != 1 or not isinstance(payload, Mapping):
        return payload
    value = dict(payload)
    handled_paths = value.get("handled_paths")
    summaries = value.get("summaries")
    if (
        value.get("status") != "success"
        or value.get("handled_count") != 1
        or not isinstance(handled_paths, list)
        or len(handled_paths) != 1
        or not isinstance(handled_paths[0], str)
        or not handled_paths[0]
        or not isinstance(summaries, list)
        or len(summaries) != 1
        or not isinstance(summaries[0], Mapping)
    ):
        return value
    summary = dict(summaries[0])
    if not isinstance(summary.get("path"), str) or not summary["path"]:
        return value
    if not isinstance(summary.get("filename"), str) or not summary["filename"]:
        return value
    controlled = request.inputs[0]
    value["handled_paths"] = [controlled.path]
    summary["path"] = controlled.path
    summary["filename"] = controlled.filename
    value["summaries"] = [summary]
    return value


class CodexSummaryProvider:
    """Run one schema-constrained summary with no user config or session state."""

    def __init__(
        self,
        config: CodexProviderConfig,
        *,
        popen_factory: PopenFactory | None = None,
        help_executor: HelpExecutor | None = None,
    ) -> None:
        self.config = config
        self._popen_factory = popen_factory or subprocess.Popen
        self._help_executor = help_executor or self._run_help

    def capability_preflight(self) -> CodexCommandResult:
        """Probe only ``codex exec --help``; it never sends a model request."""

        argv = (self.config.command, "exec", "--help")
        result = self._help_executor(argv)
        if result.returncode != 0:
            raise CodexExecutionError(
                "Codex capability preflight failed: " + _redact_diagnostic(result.stderr or result.stdout)
            )
        return result

    def summarize(self, request: CodexSummaryRequest) -> CodexSummaryResult:
        """Materialize, execute, and strictly validate one direct Codex job."""

        workdir, stdin_text = self._materialize(request)
        command = self._command_for(workdir)
        stdout = ""
        stderr = ""
        try:
            try:
                process = self._popen_factory(
                    list(command),
                    cwd=str(workdir),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    shell=False,
                    start_new_session=True,
                )
            except OSError as exc:
                raise CodexExecutionError(f"unable to start Codex: {_redact_diagnostic(str(exc))}") from exc
            try:
                stdout, stderr = _communicate(process, stdin_text, self.config.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                self._stop_process_group(process)
                diagnostic = _redact_diagnostic(_timeout_diagnostic(exc))
                raise CodexTimeoutError(
                    f"Codex summary exceeded {self.config.timeout_seconds:g}s hard timeout: {diagnostic}"
                ) from exc

            if process.returncode != 0:
                diagnostic = _redact_diagnostic("\n".join(part for part in (stdout, stderr) if part))
                raise CodexExecutionError(f"Codex exited with status {process.returncode}: {diagnostic}")
            output_path = workdir / "result.json"
            if not output_path.is_file() or output_path.is_symlink():
                raise SummaryOutputValidationError("Codex did not produce a regular output-last-message file")
            try:
                payload = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SummaryOutputValidationError("output-last-message is not one complete JSON object") from exc
            bound_payload = _bind_single_input_identity(payload, request)
            validated = validate_summary_payload(bound_payload, expected_paths=request.expected_paths)
            return CodexSummaryResult(
                output=validated,
                usage=_extract_usage(stdout),
                workdir=workdir if self.config.retain_workdir else None,
                command=command,
                diagnostics=_redact_diagnostic("\n".join(part for part in (stdout, stderr) if part)),
            )
        finally:
            if not self.config.retain_workdir:
                shutil.rmtree(workdir, ignore_errors=True)

    def _materialize(self, request: CodexSummaryRequest) -> tuple[Path, str]:
        root = Path(self.config.work_root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        safe_job = re.sub(r"[^A-Za-z0-9_.-]+", "-", request.job_id).strip(".-") or "job"
        workdir = Path(tempfile.mkdtemp(prefix=f"codex-summary-{safe_job[:48]}-", dir=root))
        try:
            inputs_dir = workdir / "inputs"
            inputs_dir.mkdir(mode=0o700)
            controlled_inputs: list[dict[str, str]] = []
            for index, item in enumerate(request.inputs, start=1):
                relative = Path("inputs") / f"{index:04d}.md"
                target = workdir / relative
                target.write_text(item.text, encoding="utf-8")
                controlled_inputs.append(
                    {"path": item.path, "filename": item.filename, "text_path": relative.as_posix()}
                )
            materialized_manifest = {
                "job_id": request.job_id,
                "manifest": dict(request.manifest),
                "inputs": controlled_inputs,
            }
            (workdir / "batch-manifest.json").write_text(
                json.dumps(materialized_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            (workdir / "prompt.md").write_text(request.prompt, encoding="utf-8")
            (workdir / "summary-system.md").write_text(request.system_prompt, encoding="utf-8")
            stdin_text = _build_stdin_prompt(request, controlled_inputs)
            return workdir, stdin_text
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise

    def _command_for(self, workdir: Path) -> tuple[str, ...]:
        schema = summary_schema_path().resolve(strict=True)
        output = (workdir / "result.json").resolve(strict=False)
        return (
            self.config.command,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            self.config.model,
            "--config",
            "model_reasoning_effort=" + json.dumps(self.config.reasoning),
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(output),
            "-",
        )

    def _stop_process_group(self, process: _Process) -> None:
        _send_process_group_signal(process, signal.SIGTERM)
        try:
            _communicate(process, None, self.config.terminate_grace_seconds)
            return
        except subprocess.TimeoutExpired:
            _send_process_group_signal(process, signal.SIGKILL)
        try:
            _communicate(process, None, max(self.config.terminate_grace_seconds, 1.0))
        except (subprocess.TimeoutExpired, OSError):
            # We have already delivered SIGKILL to the isolated group.  The
            # caller receives the hard-timeout error rather than hanging.
            pass

    def _run_help(self, argv: tuple[str, ...]) -> CodexCommandResult:
        try:
            completed = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=min(self.config.timeout_seconds, 30.0),
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodexExecutionError(f"Codex capability preflight failed: {_redact_diagnostic(str(exc))}") from exc
        return CodexCommandResult(
            argv=argv,
            returncode=completed.returncode,
            stdout=_as_text(completed.stdout),
            stderr=_as_text(completed.stderr),
        )


def _build_stdin_prompt(request: CodexSummaryRequest, controlled_inputs: Sequence[Mapping[str, str]]) -> str:
    mapping = "\n".join(
        f"- logical path `{item['path']}` / filename `{item['filename']}`: read `{item['text_path']}`"
        for item in controlled_inputs
    )
    sections = [
        "You are in a controlled, read-only summary work directory.",
        "Read only `batch-manifest.json` and the listed controlled text files. Do not read PDFs, run OCR, "
        "call Feishu/Lark, browse, or use external facts.",
        "The final response must be exactly one JSON object satisfying the supplied output schema. "
        "Do not add a prose prefix, completion notice, Markdown fence, or any extra key.",
        "Controlled input mapping:\n" + mapping,
    ]
    if request.system_prompt.strip():
        sections.append("System instructions:\n" + request.system_prompt.strip())
    sections.append("Task instructions:\n" + request.prompt.strip())
    return "\n\n".join(sections) + "\n"


def _communicate(process: _Process, input_text: str | None, timeout: float | None) -> tuple[str, str]:
    stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    return _as_text(stdout), _as_text(stderr)


def _send_process_group_signal(process: _Process, sig: signal.Signals) -> None:
    try:
        if os.name == "posix" and process.pid > 0:
            os.killpg(process.pid, sig)
        elif sig == signal.SIGKILL:
            process.kill()
        else:
            process.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        # The process may have exited naturally between timeout handling and
        # signal delivery.  It is still safe to consume/reap it below.
        pass


def _extract_usage(stdout: str) -> Mapping[str, Any]:
    """Best-effort usage extraction from JSONL events; never parse output here."""

    for line in reversed(stdout.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        found = _find_usage(event)
        if found is not None:
            return found
    return {}


def _find_usage(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        usage = value.get("usage")
        if isinstance(usage, Mapping):
            return dict(usage)
        for child in value.values():
            found = _find_usage(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_usage(child)
            if found is not None:
                return found
    return None


def _timeout_diagnostic(exc: subprocess.TimeoutExpired) -> str:
    parts = [_as_text(getattr(exc, "output", "")), _as_text(getattr(exc, "stderr", ""))]
    return "\n".join(part for part in parts if part) or "no subprocess output"


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _redact_diagnostic(text: str, *, limit: int = 4000) -> str:
    value = str(text or "")
    value = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1***", value)
    value = re.sub(
        r"(?i)(token|secret|password|authorization|cookie|session|api[_-]?key)\s*([=:])\s*([^\s,;\]}]+)",
        r"\1\2***",
        value,
    )
    return value[-limit:] or "no diagnostic output"
