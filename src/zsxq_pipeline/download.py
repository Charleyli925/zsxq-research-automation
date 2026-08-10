"""Deterministic, plan-bound ZSXQ download orchestration.

This module owns one finite source window.  It creates an immutable scan plan,
downloads only entries in that plan through one CDP session, delegates the
existing archive/reconciliation transaction to its stable Python finalizer,
and records every durable outcome in :class:`PipelineState`.
"""

from __future__ import annotations

import importlib
import argparse
import json
import os
import re
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from ._time import require_aware
from .browser import BrowserSession, BrowserSessionError, CftLaunchOptions
from .model import ErrorCategory, Stage, StageState
from .state import PipelineState


class DownloadError(RuntimeError):
    """A deterministic download transaction could not reach reconciliation."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _script_module(name: str) -> Any:
    scripts = _repo_root() / "scripts"
    if not scripts.is_dir():
        raise DownloadError(f"release scripts directory is missing: {scripts}")
    rendered = str(scripts)
    if rendered not in sys.path:
        sys.path.insert(0, rendered)
    return importlib.import_module(name)


def _parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return require_aware(value)
    normalized = str(value).strip().replace("Z", "+00:00")
    if not normalized:
        raise DownloadError("timestamp is required")
    try:
        return require_aware(datetime.fromisoformat(normalized))
    except ValueError as exc:
        raise DownloadError(f"invalid ISO-8601 timestamp: {value!r}") from exc


def _safe_source_path(source: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(source).strip()).strip(".-")
    if not cleaned:
        raise DownloadError("source name must contain a letter or digit")
    return cleaned


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_legacy_checkpoint(path: Path) -> datetime | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DownloadError(f"cannot read legacy checkpoint mirror: {path}") from exc
    value = str(payload.get("last_successful_check_at") or "").strip() if isinstance(payload, dict) else ""
    return _parse_datetime(value) if value else None


def _reason_category(reason_code: str) -> ErrorCategory:
    code = str(reason_code).strip().lower()
    if code == "need_reauth":
        return ErrorCategory.AUTH
    if code == "source_content_protected":
        return ErrorCategory.CONTENT
    if code in {"playwright_action_timeout", "zsxq_page_unavailable", "zsxq_page_state_unrecognized"}:
        return ErrorCategory.TRANSIENT
    if code.startswith(("api_", "blocked_browser_", "browser_")):
        return ErrorCategory.TRANSIENT
    return ErrorCategory.INVARIANT


def _reason_text(code: str) -> str:
    known = {
        "need_reauth": "知识星球登录态失效，未尝试下载。",
        "source_content_protected": "来源明确启用内容保护，未尝试绕过。",
        "no_new_docs": "冻结窗口内没有新文档。",
        "no_keyword_match": "冻结窗口内没有匹配关键词的文档。",
        "download_incomplete": "至少一个 immutable plan 候选未完成对账。",
        "scan_failed": "扫描阶段没有建立可执行的 immutable plan。",
        "download_completed": "所有可下载候选均已完成归档对账。",
        "plan_only": "已生成 immutable scan plan，未执行下载。",
    }
    return known.get(str(code).strip(), "下载事务未完成，请检查结构化结果和 durable stage state。")


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    source: str
    runtime_root: Path
    database: Path
    job_config_path: Path
    keyword_path: Path
    legacy_state_path: Path
    cdp_endpoint: str
    window_start: datetime
    window_end: datetime
    cft_launch_options: CftLaunchOptions | None = None
    workflow_version: str = "download:v1"
    extractor_version: str = "extract:v1"
    timeout_ms: int = 30_000
    navigation_attempts: int = 3
    plan_only: bool = False
    dry_run: bool = False
    result_path: Path | None = None
    run_id: str = ""

    def __post_init__(self) -> None:
        source = str(self.source).strip()
        if not source:
            raise ValueError("source is required")
        if not str(self.cdp_endpoint).strip():
            raise ValueError("cdp_endpoint is required")
        if int(self.timeout_ms) < 1_000:
            raise ValueError("timeout_ms must be at least 1000")
        if int(self.navigation_attempts) < 1:
            raise ValueError("navigation_attempts must be positive")
        if _parse_datetime(self.window_end) < _parse_datetime(self.window_start):
            raise ValueError("window_end must not precede window_start")


@dataclass(frozen=True, slots=True)
class DownloadOutcome:
    run_id: str
    source: str
    status: str
    reason_code: str
    reason_text: str
    window_start: datetime
    window_end: datetime
    plan_path: Path
    plan_hash: str
    manifest_path: Path | None
    plan: Mapping[str, Any]
    downloaded_entries: tuple[Mapping[str, Any], ...] = ()
    satisfied_entries: tuple[Mapping[str, Any], ...] = ()
    blocked_entries: tuple[Mapping[str, Any], ...] = ()
    missing_entries: tuple[Mapping[str, Any], ...] = ()
    checkpoint_eligible: bool = False
    state_updated: bool = False

    def to_dict(self) -> dict[str, Any]:
        downloaded_files = [str(entry.get("filename") or "").strip() for entry in self.downloaded_entries]
        archive_dirs = sorted(
            {
                str(entry.get("archive_path") or entry.get("path") or "").strip()
                for entry in self.downloaded_entries
                if str(entry.get("archive_path") or entry.get("path") or "").strip()
            }
        )
        candidate_count = int(self.plan.get("download_candidate_count") or 0)
        process_exit_code = 0 if self.status == "success" else 1
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "source": self.source,
            "status": self.status,
            "process_exit_code": process_exit_code,
            # Compatibility-only export for consumers that have not yet moved
            # to process_exit_code.  It never implies a Codex invocation.
            "codex_exit_code": process_exit_code,
            "reason_code": self.reason_code,
            "core_reason_code": self.reason_code,
            "reason_text": self.reason_text,
            "core_reason_text": self.reason_text,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "effective_window_start": self.window_start.isoformat(),
            "effective_window_end": self.window_end.isoformat(),
            "scan_plan_path": str(self.plan_path),
            "scan_plan_hash": self.plan_hash,
            "run_manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "window_new_docs_count": int(self.plan.get("window_new_docs_count") or 0),
            "keyword_matched_docs_count": int(self.plan.get("keyword_matched_docs_count") or 0),
            "download_candidate_count": candidate_count,
            "download_success_count": len(self.downloaded_entries),
            "downloaded_count": len(self.downloaded_entries),
            "downloaded_files": downloaded_files,
            "archive_dirs": archive_dirs,
            "archive_dir": archive_dirs[-1] if archive_dirs else None,
            "satisfied_candidate_count": len(self.satisfied_entries),
            "satisfied_candidates": [dict(entry) for entry in self.satisfied_entries],
            "blocked_candidate_count": len(self.blocked_entries),
            "blocked_candidates": [dict(entry) for entry in self.blocked_entries],
            "missing_candidate_count": len(self.missing_entries),
            "missing_candidates": [dict(entry) for entry in self.missing_entries],
            "no_download_reason": self.reason_code if not downloaded_files else "",
            "scan_mode": self.plan.get("scan_mode"),
            "api_probe_status": self.plan.get("api_probe_status"),
            "checkpoint_eligible": self.checkpoint_eligible,
            "state_updated": self.state_updated,
        }


FinalizerRunner = Callable[[DownloadRequest, Path, Path, str, datetime], Mapping[str, Any]]


class DownloadPipeline:
    """Run one source window without any agent, MCP, or dynamic npm dependency."""

    def __init__(
        self,
        *,
        browser_session_factory: Callable[..., BrowserSession] = BrowserSession,
        scanner: Any | None = None,
        downloader: Any | None = None,
        finalizer_runner: FinalizerRunner | None = None,
    ) -> None:
        self._browser_session_factory = browser_session_factory
        self._scanner = scanner
        self._downloader = downloader
        self._finalizer_runner = finalizer_runner or self._run_finalizer

    @property
    def scanner(self) -> Any:
        return self._scanner if self._scanner is not None else _script_module("scan_zsxq_download_candidates")

    @property
    def downloader(self) -> Any:
        return self._downloader if self._downloader is not None else _script_module("download_zsxq_plan_file")

    def resolve_window(self, request: DownloadRequest) -> tuple[datetime, datetime]:
        return _parse_datetime(request.window_start), _parse_datetime(request.window_end)

    def _plan_paths(self, request: DownloadRequest, run_id: str) -> tuple[Path, Path]:
        source = _safe_source_path(request.source)
        plan_path = request.runtime_root / "plans" / source / f"{run_id}.json"
        manifest_path = request.runtime_root / "manifests" / source / f"{run_id}.json"
        return plan_path, manifest_path

    def _record_plan_documents(
        self,
        state: PipelineState,
        *,
        request: DownloadRequest,
        source_window_id: int,
        plan: Mapping[str, Any],
    ) -> dict[str, int]:
        documents: dict[str, int] = {}
        for raw in plan.get("download_candidates") or []:
            if not isinstance(raw, Mapping):
                continue
            file_id = str(raw.get("file_id") or "").strip()
            filename = str(raw.get("filename") or raw.get("name") or "").strip()
            if not file_id or not filename:
                raise DownloadError("scan plan contains a candidate without file_id or filename")
            document = state.upsert_document(
                request.source,
                file_id,
                filename=filename,
                source_window_id=source_window_id,
            )
            state.ensure_stage(document.id, Stage.DOWNLOAD, request.workflow_version)
            documents[file_id] = document.id
        return documents

    @staticmethod
    def _terminal_blocked(result: Mapping[str, Any]) -> bool:
        return str(result.get("reason_code") or "") == "source_content_protected"

    def _run_finalizer(
        self,
        request: DownloadRequest,
        plan_path: Path,
        manifest_path: Path,
        run_id: str,
        started_at: datetime,
    ) -> Mapping[str, Any]:
        """Run the established Python finalizer with state updates disabled.

        The legacy JSON checkpoint is an input mirror only.  PipelineState is
        the authoritative checkpoint written after manifest reconciliation.
        """

        if not request.legacy_state_path.exists():
            _write_json(request.legacy_state_path, {"last_successful_check_at": request.window_start.isoformat()})
        finalizer = _script_module("finalize_download_batch")
        arguments = argparse.Namespace(
            config=str(request.job_config_path),
            keywords=str(request.keyword_path),
            state=str(request.legacy_state_path),
            window_start=request.window_start.isoformat(),
            window_end=request.window_end.isoformat(),
            downloaded_after=started_at.isoformat(),
            downloaded_before=None,
            skip_state_update=True,
            run_id=run_id,
            scan_plan=str(plan_path),
            run_manifest=str(manifest_path),
            commit_state=False,
            wait_seconds=0.0,
            wait_interval_seconds=2.0,
            dry_run=request.dry_run,
        )
        try:
            payload, exit_code = finalizer.run_finalization(arguments)
        except SystemExit as exc:
            raise DownloadError(f"finalizer rejected pipeline arguments: {exc}") from exc
        if exit_code != 0:
            raise DownloadError(f"finalizer failed ({exit_code})")
        if not isinstance(payload, dict):
            raise DownloadError("finalizer returned a non-object JSON payload")
        return payload

    def _outcome(
        self,
        *,
        run_id: str,
        request: DownloadRequest,
        plan_path: Path,
        manifest_path: Path | None,
        plan: Mapping[str, Any],
        status: str,
        reason_code: str,
        downloaded: list[Mapping[str, Any]] | None = None,
        satisfied: list[Mapping[str, Any]] | None = None,
        blocked: list[Mapping[str, Any]] | None = None,
        missing: list[Mapping[str, Any]] | None = None,
        checkpoint_eligible: bool = False,
        state_updated: bool = False,
    ) -> DownloadOutcome:
        return DownloadOutcome(
            run_id=run_id,
            source=request.source,
            status=status,
            reason_code=reason_code,
            reason_text=_reason_text(reason_code),
            window_start=request.window_start,
            window_end=request.window_end,
            plan_path=plan_path,
            plan_hash=str(plan.get("plan_hash") or ""),
            manifest_path=manifest_path,
            plan=plan,
            downloaded_entries=tuple(downloaded or []),
            satisfied_entries=tuple(satisfied or []),
            blocked_entries=tuple(blocked or []),
            missing_entries=tuple(missing or []),
            checkpoint_eligible=checkpoint_eligible,
            state_updated=state_updated,
        )

    def run(self, request: DownloadRequest) -> DownloadOutcome:
        run_id = request.run_id.strip() or str(uuid.uuid4())
        try:
            uuid.UUID(run_id)
        except ValueError as exc:
            raise DownloadError("run_id must be a UUID") from exc
        started_at = datetime.now(UTC)
        plan_path, manifest_path = self._plan_paths(request, run_id)
        scanner = self.scanner
        job_config = scanner.load_json(request.job_config_path)
        keyword_payload = scanner.load_persistent_config(request.keyword_path)

        try:
            session_kwargs: dict[str, Any] = {"connect_timeout_ms": request.timeout_ms}
            if request.cft_launch_options is not None:
                session_kwargs["cft_launch_options"] = request.cft_launch_options
            with self._browser_session_factory(request.cdp_endpoint, **session_kwargs) as session:
                plan = scanner.scan_window(
                    session.page,
                    window_start=request.window_start,
                    window_end=request.window_end,
                    job_config=job_config,
                    keyword_payload=keyword_payload,
                )
                if not isinstance(plan, dict):
                    raise DownloadError("scanner returned a non-object plan")
                _write_json(plan_path, plan)
                blocked_reason = str(plan.get("blocked_reason") or "").strip()
                if blocked_reason:
                    with PipelineState.open(request.database) as state:
                        state.migrate()
                        if not request.plan_only:
                            state.register_source_window(
                                request.source,
                                request.window_start,
                                request.window_end,
                                status="blocked",
                                checkpoint_eligible=False,
                            )
                    outcome = self._outcome(
                        run_id=run_id,
                        request=request,
                        plan_path=plan_path,
                        manifest_path=None,
                        plan=plan,
                        status="blocked",
                        reason_code=blocked_reason,
                    )
                    if request.result_path:
                        _write_json(request.result_path, outcome.to_dict())
                    return outcome

                if request.plan_only:
                    outcome = self._outcome(
                        run_id=run_id,
                        request=request,
                        plan_path=plan_path,
                        manifest_path=None,
                        plan=plan,
                        status="success",
                        reason_code="plan_only",
                    )
                    if request.result_path:
                        _write_json(request.result_path, outcome.to_dict())
                    return outcome

                candidates = [item for item in plan.get("download_candidates") or [] if isinstance(item, Mapping)]
                with PipelineState.open(request.database) as state:
                    state.migrate()
                    source_window_id = state.register_source_window(
                        request.source,
                        request.window_start,
                        request.window_end,
                        status="running",
                        checkpoint_eligible=False,
                    )
                    document_ids = self._record_plan_documents(
                        state,
                        request=request,
                        source_window_id=source_window_id,
                        plan=plan,
                    )
                    if not candidates:
                        state.register_source_window(
                            request.source,
                            request.window_start,
                            request.window_end,
                            status="succeeded",
                            checkpoint_eligible=True,
                        )
                        reason = "no_new_docs" if int(plan.get("window_new_docs_count") or 0) == 0 else "no_keyword_match"
                        outcome = self._outcome(
                            run_id=run_id,
                            request=request,
                            plan_path=plan_path,
                            manifest_path=None,
                            plan=plan,
                            status="success",
                            reason_code=reason,
                            checkpoint_eligible=True,
                            state_updated=True,
                        )
                        if request.result_path:
                            _write_json(request.result_path, outcome.to_dict())
                        return outcome

                    attempted: dict[str, Mapping[str, Any]] = {}
                    claims: dict[str, Any] = {}
                    blocked: list[Mapping[str, Any]] = []
                    satisfied: list[Mapping[str, Any]] = []
                    for candidate in candidates:
                        file_id = str(candidate.get("file_id") or "").strip()
                        document_id = document_ids.get(file_id)
                        if document_id is None:
                            raise DownloadError(f"scan plan document is not registered: {file_id}")
                        claim = state.claim_due_stage(
                            Stage.DOWNLOAD,
                            request.workflow_version,
                            document_ids=(document_id,),
                            lease_seconds=max(60, int(request.timeout_ms / 1000) + 60),
                        )
                        if claim is None:
                            prior = state.get_stage_attempt(document_id, Stage.DOWNLOAD, request.workflow_version)
                            if prior and prior.get("state") == StageState.SUCCEEDED.value:
                                satisfied.append({"source_file_id": file_id, "filename": candidate.get("filename"), "disposition": "already_completed"})
                                continue
                            attempted[file_id] = {
                                "status": "blocked",
                                "reason_code": "download_stage_busy",
                                "file_id": file_id,
                                "filename": candidate.get("filename"),
                            }
                            continue
                        claims[file_id] = claim
                        result = self.downloader.download_candidate_on_page(
                            dict(candidate),
                            page=session.page,
                            staging_dir=self.downloader.choose_staging_dir(job_config),
                            timeout_ms=request.timeout_ms,
                            group_url=str(job_config.get("group_url") or "").strip(),
                            group_name=str(job_config.get("group_name") or "前沿信息收录").strip(),
                            tag_name=str(job_config.get("tag_name") or "").strip(),
                            navigation_attempts=request.navigation_attempts,
                        )
                        attempted[file_id] = result
                        if str(result.get("status") or "") == "blocked":
                            category = _reason_category(str(result.get("reason_code") or ""))
                            state.fail_stage(
                                claim,
                                category=category,
                                error_code=str(result.get("reason_code") or "download_blocked"),
                                error_detail=str(result.get("message") or ""),
                                retry_at=datetime.now(UTC) + timedelta(minutes=5) if category is ErrorCategory.TRANSIENT else None,
                            )
                            blocked.append(result)
                            claims.pop(file_id, None)

                    downloaded_results = [item for item in attempted.values() if str(item.get("status") or "") == "downloaded"]
                    finalizer_summary: Mapping[str, Any] = {}
                    if downloaded_results:
                        try:
                            finalizer_summary = self._finalizer_runner(request, plan_path, manifest_path, run_id, started_at)
                        except Exception as exc:
                            for file_id, claim in list(claims.items()):
                                state.fail_stage(
                                    claim,
                                    category=ErrorCategory.TRANSIENT,
                                    error_code="finalizer_failed",
                                    error_detail=str(exc),
                                    retry_at=datetime.now(UTC) + timedelta(minutes=5),
                                )
                                claims.pop(file_id, None)
                            raise

                    downloaded_entries = [item for item in finalizer_summary.get("files") or [] if isinstance(item, Mapping)]
                    if not downloaded_entries and manifest_path.is_file():
                        try:
                            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                        except ValueError:
                            manifest_payload = {}
                        downloaded_entries = [
                            item for item in manifest_payload.get("downloaded_entries") or [] if isinstance(item, Mapping)
                        ]
                    entries_by_file_id = {
                        str(item.get("source_file_id") or "").strip(): item
                        for item in downloaded_entries
                        if str(item.get("source_file_id") or "").strip()
                    }
                    finalizer_satisfied = [
                        item for item in finalizer_summary.get("satisfied_candidates") or [] if isinstance(item, Mapping)
                    ]
                    satisfied_ids = {
                        str(item.get("source_file_id") or "").strip()
                        for item in finalizer_satisfied
                        if str(item.get("source_file_id") or "").strip()
                    }
                    satisfied.extend(finalizer_satisfied)

                    missing: list[Mapping[str, Any]] = []
                    for file_id, claim in list(claims.items()):
                        entry = entries_by_file_id.get(file_id)
                        if entry is not None:
                            document_id = document_ids[file_id]
                            artifact_path = Path(str(entry.get("archive_path") or entry.get("path") or "")).expanduser()
                            if not artifact_path.is_file():
                                state.fail_stage(
                                    claim,
                                    category=ErrorCategory.INVARIANT,
                                    error_code="finalizer_artifact_missing",
                                    error_detail=str(artifact_path),
                                )
                                missing.append({"source_file_id": file_id, "filename": entry.get("filename"), "reason_code": "finalizer_artifact_missing"})
                            else:
                                artifact = state.record_artifact(
                                    document_id,
                                    kind="pdf",
                                    path=artifact_path,
                                    pdf_sha256=str(entry.get("pdf_sha256") or "") or None,
                                    size_bytes=artifact_path.stat().st_size,
                                    metadata={"plan_hash": plan.get("plan_hash"), "run_id": run_id},
                                )
                                state.complete_stage(claim, output_artifact_id=artifact.id)
                                state.ensure_stage(document_id, Stage.TEXT_EXTRACT, request.extractor_version)
                        elif file_id in satisfied_ids:
                            state.complete_stage(claim)
                        else:
                            result = attempted.get(file_id, {})
                            code = str(result.get("reason_code") or "browser_download_unstable")
                            state.fail_stage(
                                claim,
                                category=ErrorCategory.TRANSIENT,
                                error_code=code,
                                error_detail="candidate was not present in finalizer reconciliation",
                                retry_at=datetime.now(UTC) + timedelta(minutes=5),
                            )
                            missing.append({"source_file_id": file_id, "filename": result.get("filename"), "reason_code": code})

                    terminal_blocked = [entry for entry in blocked if self._terminal_blocked(entry)]
                    nonterminal_blocked = [entry for entry in blocked if not self._terminal_blocked(entry)]
                    missing.extend(nonterminal_blocked)
                    checkpoint_eligible = not missing
                    state.register_source_window(
                        request.source,
                        request.window_start,
                        request.window_end,
                        status="succeeded" if checkpoint_eligible else "partial",
                        checkpoint_eligible=checkpoint_eligible,
                    )
                    status = "success" if checkpoint_eligible else ("partial" if downloaded_entries or satisfied else "failed")
                    reason = "download_incomplete" if missing else ("source_content_protected" if terminal_blocked else "download_completed")
                    outcome = self._outcome(
                        run_id=run_id,
                        request=request,
                        plan_path=plan_path,
                        manifest_path=manifest_path if downloaded_results else None,
                        plan=plan,
                        status=status,
                        reason_code=reason,
                        downloaded=downloaded_entries,
                        satisfied=satisfied,
                        blocked=blocked,
                        missing=missing,
                        checkpoint_eligible=checkpoint_eligible,
                        state_updated=True,
                    )
                    if request.result_path:
                        _write_json(request.result_path, outcome.to_dict())
                    return outcome
        except BrowserSessionError as exc:
            plan: Mapping[str, Any] = {
                "schema_version": 3,
                "window_start": request.window_start.isoformat(),
                "window_end": request.window_end.isoformat(),
                "plan_hash": "",
                "download_candidate_count": 0,
            }
            _write_json(plan_path, plan)
            outcome = self._outcome(
                run_id=run_id,
                request=request,
                plan_path=plan_path,
                manifest_path=None,
                plan=plan,
                status="blocked",
                reason_code=exc.code,
            )
            if request.result_path:
                _write_json(request.result_path, outcome.to_dict())
            return outcome
