"""Direct, injectable ``lark-cli`` adapters for documents and notifications.

Remote writes deliberately stop at :meth:`LarkPublisher.create_document` or
:meth:`LarkPublisher.append_document`.  The orchestration layer must persist
``remote_written`` before it invokes title, fetch, or permission verification;
this prevents a crash during verification from issuing a duplicate document
write on recovery.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class LarkError(RuntimeError):
    """Base error for direct lark-cli adapters."""


class LarkTimeoutError(LarkError):
    """A lark-cli invocation exceeded its hard subprocess timeout."""


class LarkCommandError(LarkError):
    """lark-cli returned a non-zero exit status with a redacted diagnostic."""

    def __init__(self, operation: str, returncode: int, diagnostic: str) -> None:
        self.operation = operation
        self.returncode = returncode
        self.diagnostic = redact_diagnostic(diagnostic)
        super().__init__(f"lark-cli {operation} failed with status {returncode}: {self.diagnostic}")


class LarkProtocolError(LarkError):
    """A successful lark-cli call did not return the expected JSON contract."""


class LarkCapabilityError(LarkError):
    """A read-only ``--help`` capability probe failed."""


@dataclass(frozen=True, slots=True)
class LarkCliConfig:
    """Stable adapter configuration, intentionally independent from pipeline TOML."""

    command: str = "lark-cli"
    config_dir: Path | str | None = None
    timeout_seconds: float = 90.0
    api_version: str = "v2"
    document_url_base: str = "https://feishu.cn/docx"
    parent_position: str | None = None
    user_identity: str = "user"
    bot_identity: str = "bot"

    def __post_init__(self) -> None:
        if not str(self.command).strip():
            raise ValueError("lark-cli command is required")
        if self.timeout_seconds <= 0:
            raise ValueError("lark-cli timeout_seconds must be positive")
        if not str(self.api_version).strip():
            raise ValueError("lark-cli api_version is required")
        if self.user_identity != "user":
            raise ValueError("document operations must use the user identity")
        if self.bot_identity != "bot":
            raise ValueError("notification operations must use the bot identity")
        base = str(self.document_url_base).strip().rstrip("/")
        if not re.fullmatch(r"https?://[^\s]+", base):
            raise ValueError("document_url_base must be an HTTP(S) URL")
        if self.config_dir is not None:
            config_dir = Path(self.config_dir).expanduser()
            if not config_dir.is_absolute():
                raise ValueError("lark-cli config_dir must be an absolute path when provided")
            object.__setattr__(self, "config_dir", config_dir.resolve(strict=False))
        object.__setattr__(self, "command", str(self.command).strip())
        object.__setattr__(self, "api_version", str(self.api_version).strip())
        object.__setattr__(self, "document_url_base", base)


@dataclass(frozen=True, slots=True)
class LarkCommandResult:
    """A command result that intentionally excludes environment data."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True, slots=True)
class RemoteDocument:
    """Typed remote document projection returned by document operations."""

    url: str
    title: str = ""
    body: str = ""
    mode: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class NotificationReceipt:
    """A successful bot notification, keyed for safe remote de-duplication."""

    idempotency_key: str
    message_id: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


class LarkExecutor(Protocol):
    """Injectable subprocess boundary used by all tests and capability probes."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> LarkCommandResult: ...


PermissionArgvBuilder = Callable[[str, str, str], Sequence[str]]


class _LarkClient:
    def __init__(self, config: LarkCliConfig, *, executor: LarkExecutor | None = None) -> None:
        self.config = config
        self._executor = executor or _run_subprocess

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        # The JSON stdout contract is unusable if a version/skills reminder is
        # printed into it.  These values are intentionally set for every call.
        environment["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        environment["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
        if self.config.config_dir is not None:
            environment["LARKSUITE_CLI_CONFIG_DIR"] = str(self.config.config_dir)
        return environment

    def _execute(
        self,
        operation: str,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> LarkCommandResult:
        argv = (self.config.command, *tuple(str(argument) for argument in arguments))
        try:
            result = self._executor(
                argv,
                input_text=input_text,
                environment=self._environment(),
                timeout_seconds=self.config.timeout_seconds,
            )
        except LarkError:
            raise
        except subprocess.TimeoutExpired as exc:
            raise LarkTimeoutError(
                f"lark-cli {operation} timed out after {self.config.timeout_seconds:g}s: "
                f"{redact_diagnostic(_timeout_output(exc))}"
            ) from exc
        except OSError as exc:
            raise LarkError(f"unable to start lark-cli {operation}: {redact_diagnostic(str(exc))}") from exc
        if not isinstance(result, LarkCommandResult):
            raise TypeError("lark executor must return LarkCommandResult")
        if result.returncode != 0:
            raise LarkCommandError(operation, result.returncode, "\n".join((result.stdout, result.stderr)))
        return result

    def _execute_json(
        self,
        operation: str,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> Mapping[str, Any] | list[Any]:
        result = self._execute(operation, arguments, input_text=input_text)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise LarkProtocolError(
                f"lark-cli {operation} returned invalid JSON: {redact_diagnostic(result.stdout)}"
            ) from exc
        if not isinstance(payload, (dict, list)):
            raise LarkProtocolError(f"lark-cli {operation} JSON root must be an object or array")
        return payload

    def _preflight(self, operation: str, arguments: Sequence[str]) -> LarkCommandResult:
        try:
            return self._execute(operation, arguments)
        except LarkError as exc:
            raise LarkCapabilityError(str(exc)) from exc


class LarkPublisher(_LarkClient):
    """Create, append, inspect, fetch, title, and grant Lark documents as user."""

    def __init__(
        self,
        config: LarkCliConfig,
        *,
        executor: LarkExecutor | None = None,
        permission_argv_builder: PermissionArgvBuilder | None = None,
    ) -> None:
        super().__init__(config, executor=executor)
        self._permission_argv_builder = permission_argv_builder or _default_permission_argv

    def capability_preflight(self) -> tuple[LarkCommandResult, ...]:
        """Run only CLI help commands; no document or permission is modified."""

        probes = (
            ("docs create", ("docs", "+create", "--help")),
            ("docs append", ("docs", "+update", "--help")),
            ("docs fetch", ("docs", "+fetch", "--help")),
            ("drive title", ("drive", "files", "patch", "--help")),
            ("drive inspect", ("drive", "+inspect", "--help")),
            ("drive permission", ("drive", "permission.members", "create", "--help")),
        )
        return tuple(self._preflight(operation, arguments) for operation, arguments in probes)

    def create_document(
        self,
        markdown: str | None = None,
        *,
        title: str = "",
        parent_position: str | None = None,
    ) -> RemoteDocument:
        """Write a new document, then return before state/verification work.

        The caller must record ``remote_written`` after this returns and before
        calling :meth:`set_title`, :meth:`fetch_document`, or
        :meth:`grant_chat_view`.
        """

        content = _require_nonempty(markdown or "", "document markdown", preserve=True)
        # ``title`` is accepted for the publication-layer protocol but is not
        # applied here.  A document write must return first so the caller can
        # durably record remote_written before title patch/inspection.
        if title:
            _require_nonempty(title, "document title")
        position = parent_position if parent_position is not None else self.config.parent_position
        arguments: list[str] = [
            "docs",
            "+create",
            "--api-version",
            self.config.api_version,
            "--as",
            self.config.user_identity,
            "--content",
            "-",
            "--doc-format",
            "markdown",
        ]
        if position:
            arguments.extend(("--parent-position", str(position)))
        arguments.append("--json")
        payload = self._execute_json("docs create", arguments, input_text=content)
        return _document_from_payload(payload, base_url=self.config.document_url_base, mode="create")

    def append_document(
        self,
        document_url: str | None = None,
        markdown: str | None = None,
        *,
        document: str | None = None,
    ) -> RemoteDocument:
        """Append Markdown to one existing document and return before verification."""

        url = _resolve_document_url(document_url, document)
        content = _require_nonempty(markdown or "", "document markdown", preserve=True)
        arguments = (
            "docs",
            "+update",
            "--api-version",
            self.config.api_version,
            "--as",
            self.config.user_identity,
            "--doc",
            url,
            "--command",
            "append",
            "--content",
            "-",
            "--doc-format",
            "markdown",
            "--json",
        )
        payload = self._execute_json("docs append", arguments, input_text=content)
        return _document_from_payload(payload, base_url=self.config.document_url_base, fallback_url=url, mode="append")

    def set_title(
        self,
        document_url: str | None = None,
        title: str | None = None,
        *,
        document: str | None = None,
    ) -> RemoteDocument:
        """Update a newly created document title as the user identity."""

        url = _resolve_document_url(document_url, document)
        expected_title = _require_nonempty(title or "", "document title")
        token = document_token(url)
        parameters = _compact_json({"file_token": token, "type": "docx"})
        data = _compact_json({"new_title": expected_title})
        payload = self._execute_json(
            "drive title",
            (
                "drive",
                "files",
                "patch",
                "--as",
                self.config.user_identity,
                "--params",
                parameters,
                "--data",
                data,
                "--json",
            ),
        )
        return RemoteDocument(
            url=url,
            title=_find_text(payload, ("title", "name")) or expected_title,
            mode="title",
            raw=_as_mapping(payload),
        )

    def inspect_document(self, document_url: str | None = None, *, document: str | None = None) -> RemoteDocument:
        """Fetch document metadata for title verification without writing."""

        url = _resolve_document_url(document_url, document)
        payload = self._execute_json(
            "drive inspect",
            ("drive", "+inspect", "--as", self.config.user_identity, "--url", url, "--json"),
        )
        return RemoteDocument(
            url=_extract_document_url(payload, self.config.document_url_base) or url,
            title=_find_text(payload, ("title", "name")),
            body=_find_text(payload, ("content", "body")),
            mode="inspect",
            raw=_as_mapping(payload),
        )

    def fetch_document(self, document_url: str | None = None, *, document: str | None = None) -> RemoteDocument:
        """Fetch one document body as user for post-write anchor verification."""

        url = _resolve_document_url(document_url, document)
        payload = self._execute_json(
            "docs fetch",
            (
                "docs",
                "+fetch",
                "--api-version",
                self.config.api_version,
                "--as",
                self.config.user_identity,
                "--doc",
                url,
                "--json",
            ),
        )
        return RemoteDocument(
            url=_extract_document_url(payload, self.config.document_url_base) or url,
            title=_find_text(payload, ("title", "name")),
            body=_find_text(payload, ("content", "body", "markdown")),
            mode="fetch",
            raw=_as_mapping(payload),
        )

    def verify_title(
        self,
        document_url: str | None = None,
        expected_title: str | None = None,
        *,
        document: str | None = None,
        title: str | None = None,
    ) -> RemoteDocument:
        """Fail closed unless drive inspection returns exactly the expected title."""

        expected = _require_nonempty(title if title is not None else expected_title or "", "expected document title")
        fetched_document = self.inspect_document(document_url, document=document)
        if fetched_document.title != expected:
            raise LarkProtocolError(
                "document title verification failed: "
                f"expected={redact_diagnostic(expected, limit=240)!r} "
                f"actual={redact_diagnostic(fetched_document.title, limit=240)!r}"
            )
        return fetched_document

    def verify_body(
        self,
        document_url: str | None = None,
        expected_markdown: str | None = None,
        *,
        document: str | None = None,
    ) -> RemoteDocument:
        """Fetch and require meaningful local-Markdown anchors in the remote body."""

        expected = _require_nonempty(expected_markdown or "", "expected document markdown", preserve=True)
        fetched_document = self.fetch_document(document_url, document=document)
        anchors = _markdown_anchors(expected)
        actual = _compact_text(fetched_document.body)
        expected_compact = _compact_text(expected)
        min_chars = min(200, max(60, len(expected_compact) // 8))
        matched = [anchor for anchor in anchors if _compact_text(anchor) in actual]
        min_matches = 1 if len(anchors) <= 2 else 2
        if len(actual) < min_chars or len(matched) < min_matches:
            raise LarkProtocolError(
                "document body verification failed: fetched body did not contain enough local summary anchors "
                f"(fetched_chars={len(actual)}, matched_anchors={len(matched)}/{len(anchors)})"
            )
        return fetched_document

    def grant_chat_view(
        self,
        document_url: str | None = None,
        chat_id: str | None = None,
        *,
        document: str | None = None,
    ) -> None:
        """Grant target chat view access with the documented user-side command."""

        url = _resolve_document_url(document_url, document)
        chat = _require_nonempty(chat_id or "", "target chat_id")
        arguments = tuple(self._permission_argv_builder(url, chat, self.config.user_identity))
        if not arguments:
            raise ValueError("permission argv builder returned no arguments")
        self._execute_json("drive permission", arguments)

    def set_permissions(self, *, document: str, chat_id: str) -> None:
        """Publication-layer compatibility alias for :meth:`grant_chat_view`."""

        self.grant_chat_view(document=document, chat_id=chat_id)


class LarkNotifier(_LarkClient):
    """Send one bot notification with a mandatory remote idempotency key."""

    def capability_preflight(self) -> LarkCommandResult:
        """Run only ``im +messages-send --help``; it never posts a message."""

        return self._preflight("im notify", ("im", "+messages-send", "--help"))

    def notify_once(self, chat_id: str, markdown: str, *, idempotency_key: str) -> NotificationReceipt:
        """Send Markdown as bot; retry callers reuse the same key, never a fallback."""

        chat = _require_nonempty(chat_id, "target chat_id")
        content = _require_nonempty(markdown, "notification markdown", preserve=True)
        key = _require_nonempty(idempotency_key, "notification idempotency_key")
        payload = self._execute_json(
            "im notify",
            (
                "im",
                "+messages-send",
                "--chat-id",
                chat,
                "--idempotency-key",
                key,
                "--as",
                self.config.bot_identity,
                "--markdown",
                content,
                "--json",
            ),
        )
        return NotificationReceipt(
            idempotency_key=key,
            message_id=_find_text(payload, ("message_id",)),
            raw=_as_mapping(payload),
        )


def _run_subprocess(
    argv: Sequence[str],
    *,
    input_text: str | None,
    environment: Mapping[str, str],
    timeout_seconds: float,
) -> LarkCommandResult:
    """The only real subprocess boundary: argv array, no shell, finite timeout."""

    completed = subprocess.run(
        list(argv),
        input=input_text,
        capture_output=True,
        text=True,
        env=dict(environment),
        timeout=timeout_seconds,
        check=False,
        shell=False,
    )
    return LarkCommandResult(
        argv=tuple(str(item) for item in argv),
        returncode=completed.returncode,
        stdout=_as_text(completed.stdout),
        stderr=_as_text(completed.stderr),
    )


def _default_permission_argv(document_url: str, chat_id: str, identity: str) -> Sequence[str]:
    token = document_token(document_url)
    data = _compact_json(
        {"member_type": "openchat", "member_id": chat_id, "perm": "view", "type": "chat"}
    )
    return (
        "drive",
        "permission.members",
        "create",
        "--as",
        identity,
        "--token",
        token,
        "--type",
        "docx",
        "--data",
        data,
        "--yes",
        "--json",
    )


def document_token(document_url: str) -> str:
    """Extract the opaque doc token without ever interpolating it into a shell."""

    url = _require_document_url(document_url)
    token = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,}", token):
        raise ValueError("document URL does not contain a valid document token")
    return token


def redact_diagnostic(text: str, *, limit: int = 1200) -> str:
    """Keep failures useful while excluding credentials and long remote bodies."""

    value = str(text or "")
    value = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1***", value)
    value = re.sub(r"(?i)(cli_)[A-Za-z0-9_-]+", r"\1***", value)
    value = re.sub(
        r"(?i)([\"']?(?:tenant[_-]?access[_-]?token|access[_-]?token|refresh[_-]?token|"
        r"app[_-]?secret|secret|password|api[_-]?key|cookie|session|authorization)[\"']?\s*[:=]\s*[\"']?)([^\"'\s,;\]}]+)",
        r"\1***",
        value,
    )
    return value[-limit:] or "no diagnostic output"


def _document_from_payload(
    payload: Mapping[str, Any] | list[Any],
    *,
    base_url: str,
    fallback_url: str = "",
    mode: str,
) -> RemoteDocument:
    url = _extract_document_url(payload, base_url) or fallback_url
    if not url:
        raise LarkProtocolError("lark-cli document response did not contain a document URL")
    return RemoteDocument(
        url=url,
        title=_find_text(payload, ("title", "name")),
        body=_find_text(payload, ("content", "body", "markdown")),
        mode=mode,
        raw=_as_mapping(payload),
    )


def _extract_document_url(value: Any, base_url: str) -> str:
    for candidate in _iter_text_for_keys(value, ("doc_url", "document_url", "url", "doc_token", "document_id")):
        normalized = _normalize_document_url(candidate, base_url)
        if normalized:
            return normalized
    return ""


def _normalize_document_url(value: str, base_url: str) -> str:
    candidate = str(value or "").strip().strip("\"'")
    if re.fullmatch(r"https?://[^\s]+", candidate):
        return candidate
    if re.fullmatch(r"[A-Za-z0-9_-]{8,}", candidate):
        return f"{base_url}/{candidate}"
    return ""


def _find_text(value: Any, keys: Sequence[str]) -> str:
    for candidate in _iter_text_for_keys(value, keys):
        if candidate.strip():
            return candidate.strip()
    return ""


def _iter_text_for_keys(value: Any, keys: Sequence[str]):
    wanted = set(keys)
    if isinstance(value, Mapping):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str):
                yield candidate
        for key, child in value.items():
            if key not in wanted:
                yield from _iter_text_for_keys(child, keys)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_text_for_keys(child, keys)


def _as_mapping(value: Mapping[str, Any] | list[Any]) -> Mapping[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {"items": value}


def _compact_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _require_nonempty(value: str, field: str, *, preserve: bool = False) -> str:
    raw = str(value or "")
    if not raw.strip():
        raise ValueError(f"{field} is required")
    return raw if preserve else raw.strip()


def _require_document_url(value: str) -> str:
    url = _require_nonempty(value, "document URL")
    if not re.fullmatch(r"https?://[^\s]+", url):
        raise ValueError("document URL must be an HTTP(S) URL")
    return url


def _resolve_document_url(document_url: str | None, document: str | None) -> str:
    """Accept the direct positional API and the temporary protocol keyword."""

    direct = str(document_url or "").strip()
    compatibility = str(document or "").strip()
    if direct and compatibility and direct != compatibility:
        raise ValueError("document_url and document must not disagree")
    return _require_document_url(direct or compatibility)


def _timeout_output(exc: subprocess.TimeoutExpired) -> str:
    return "\n".join(
        part for part in (_as_text(getattr(exc, "output", "")), _as_text(getattr(exc, "stderr", ""))) if part
    ) or "no subprocess output"


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _compact_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[#>*_`~\-•]+", " ", text)
    return re.sub(r"\s+", "", text)


def _markdown_anchors(markdown: str) -> list[str]:
    generic = {"核心结论", "核心问题与回答", "摘要", "本地文件"}
    anchors: list[str] = []
    for raw_line in markdown.splitlines():
        line = re.sub(r"^\s{0,3}(#{1,6}|[-*+]|>\s*)\s*", "", raw_line.strip())
        line = re.sub(r"^\d+[.)]\s*", "", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"[*_`]+", "", line).strip()
        if not line or line in generic or len(line) < 12:
            continue
        anchors.append(line)
        if len(anchors) >= 8:
            break
    if anchors:
        return anchors
    compact = _compact_text(markdown)
    return [compact[:80]] if compact else []
