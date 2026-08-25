"""Bounded, one-shot orchestration for the unified local pipeline scheduler."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ._time import from_iso
from .browser import CftLaunchOptions
from .config import PipelineConfig, SourceConfig
from .download import DownloadOutcome, DownloadPipeline, DownloadRequest
from .lark import LarkCliConfig, LarkNotifier
from .lock import runtime_lock
from .notify import (
    BATCH_COMPLETE_EVENT,
    DOWNLOAD_BLOCKED_EVENT,
    DOWNLOAD_COMPLETE_EVENT,
    SUMMARY_PROGRESS_EVENT,
    SUMMARY_STARTED_EVENT,
    NotificationDelivery,
    NotificationDrainer,
    enqueue_pipeline_status_notification,
    export_notification_audit,
    render_batch_complete_notice,
    render_download_blocked_notice,
    render_download_complete_notice,
    render_summary_progress_notice,
    render_summary_started_notice,
)
from .process import DigestProcessor, ProcessConfig, ProcessOutcome, ProcessRequest
from .scheduler import PipelineScheduler, ScheduledWindow
from .state import PipelineState


class WorkerError(RuntimeError):
    """The unified worker cannot safely construct one requested stage."""


@dataclass(frozen=True, slots=True)
class TickOutcome:
    """Sanitized result of one bounded manual or launchd invocation."""

    status: str
    scheduled: tuple[ScheduledWindow, ...] = ()
    downloaded: int = 0
    processed: int = 0
    notifications: tuple[NotificationDelivery, ...] = ()
    budget_exhausted: bool = False
    failures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "scheduled": [window.to_dict() for window in self.scheduled],
            "downloaded": self.downloaded,
            "processed": self.processed,
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
            "budget_exhausted": self.budget_exhausted,
            "failures": list(self.failures),
        }


DownloadRunner = Callable[[DownloadRequest], DownloadOutcome]
ProcessRunner = Callable[[str, tuple[Mapping[str, Any], ...]], ProcessOutcome]
OutboxRunner = Callable[[int], tuple[NotificationDelivery, ...]]


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
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


class PipelineWorker:
    """Run due download and processing work without PID-file inference.

    A single runtime-wide flock serialises ``tick`` and every manual
    ``run-stage``/``outbox drain`` command.  SQLite remains responsible for
    individual document claims and external-side-effect recovery.
    """

    def __init__(
        self,
        config: PipelineConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        download_runner: DownloadRunner | None = None,
        process_runner: ProcessRunner | None = None,
        outbox_runner: OutboxRunner | None = None,
    ) -> None:
        self.config = config
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.monotonic = monotonic
        self._scheduler = PipelineScheduler(config, clock=self.clock)
        self._download_runner = download_runner or DownloadPipeline().run
        self._process_runner = process_runner or self._run_process
        self._outbox_runner = outbox_runner or self._drain_outbox

    def tick(self, *, budget_seconds: int | None = None) -> TickOutcome:
        """Discover due slots and keep durable notifications ahead of long work."""

        return self._run(stages=("schedule", "download", "process", "outbox"), budget_seconds=budget_seconds)

    def run_stage(self, stage: str, *, budget_seconds: int | None = None) -> TickOutcome:
        """Run one worker stage under the same lock/configuration as ``tick``."""

        normalized = str(stage).strip().lower()
        accepted = {
            "download": ("download",),
            "process": ("process",),
            "outbox": ("outbox",),
            "all": ("download", "process", "outbox"),
        }
        if normalized not in accepted:
            raise WorkerError("run-stage must be download, process, outbox, or all")
        return self._run(stages=accepted[normalized], budget_seconds=budget_seconds)

    def _run(self, *, stages: tuple[str, ...], budget_seconds: int | None) -> TickOutcome:
        budget = int(budget_seconds if budget_seconds is not None else self.config.schedule.tick_budget_seconds)
        if budget < 1:
            raise WorkerError("budget_seconds must be positive")
        with runtime_lock(self.config.runtime.root) as acquired:
            if not acquired:
                return TickOutcome(status="busy")
            return self._run_locked(stages=stages, deadline=self.monotonic() + budget)

    def _expired(self, deadline: float) -> bool:
        return self.monotonic() >= deadline

    def _run_locked(self, *, stages: tuple[str, ...], deadline: float) -> TickOutcome:
        scheduled: tuple[ScheduledWindow, ...] = ()
        failures: list[str] = []
        downloaded = 0
        processed = 0
        notifications_by_key: dict[str, NotificationDelivery] = {}
        budget_exhausted = False

        def drain_outbox() -> None:
            try:
                deliveries = self._outbox_runner(self.config.schedule.outbox_quota)
            except Exception as exc:
                failures.append(f"outbox:{type(exc).__name__}")
                return
            for delivery in deliveries:
                notifications_by_key[delivery.idempotency_key] = delivery

        with PipelineState.open(self.config.runtime.database) as state:
            state.migrate()
            if "schedule" in stages:
                scheduled = self._scheduler.enqueue_due_windows(state, now=self.clock())

        if "download" in stages:
            with PipelineState.open(self.config.runtime.database) as state:
                state.migrate()
                pending_windows = state.list_source_windows(
                    statuses=("scheduled",), limit=self.config.schedule.download_quota
                )
            for row in pending_windows:
                if self._expired(deadline):
                    budget_exhausted = True
                    break
                try:
                    outcome = self._run_download_window(row)
                except Exception as exc:
                    # A failure in one source is visible but must not stop a
                    # different source or a downstream recovery stage.
                    failures.append(f"download:{str(row.get('source') or 'unknown')}:{type(exc).__name__}")
                    continue
                if outcome is None:
                    # An earlier coalesced window already advanced the durable
                    # checkpoint beyond this pending window's end. Marking it
                    # settled avoids an overlapping browser replay.
                    continue
                if outcome.status == "success":
                    downloaded += 1
                    try:
                        self._queue_download_complete(row, outcome)
                    except Exception as exc:
                        failures.append(f"notify:{outcome.source}:download_complete:{type(exc).__name__}")
                elif outcome.status != "busy":
                    reason_code = str(getattr(outcome, "reason_code", "") or "").strip()
                    suffix = f":{reason_code}" if reason_code else ""
                    failures.append(f"download:{outcome.source}:{outcome.status}{suffix}")
                    if outcome.status == "blocked" and reason_code:
                        try:
                            self._queue_download_blocked(row, outcome)
                        except Exception as exc:
                            failures.append(f"notify:{outcome.source}:download_blocked:{type(exc).__name__}")

        # A processor can legitimately run beyond the soft tick deadline while
        # finishing a model request.  Drain durable work from the prior tick
        # first so a sustained processing backlog cannot starve notifications.
        # A second drain below still sends notifications produced by this tick
        # when processing finishes inside the budget.
        if "outbox" in stages and "process" in stages:
            if not self._expired(deadline):
                drain_outbox()
            else:
                budget_exhausted = True

        if "process" in stages and not self._expired(deadline):
            remaining = self.config.schedule.process_quota
            for source in self.config.sources.values():
                if remaining < 1 or self._expired(deadline):
                    budget_exhausted = self._expired(deadline)
                    break
                with PipelineState.open(self.config.runtime.database) as state:
                    state.migrate()
                    rows = state.list_documents_for_processing(
                        source.name,
                        extractor_workflow=f"extract:{self._extractor_version()}",
                        limit=remaining,
                    )
                if not rows:
                    continue
                try:
                    queued_summary_start = self._queue_summary_starts(rows)
                except Exception as exc:
                    failures.append(f"notify:{source.name}:summary_started:{type(exc).__name__}")
                    queued_summary_start = False
                # A start message is useful only before the potentially long
                # model stage.  The durable outbox still keeps processing
                # independent if Lark is temporarily unavailable.
                if queued_summary_start and "outbox" in stages and not self._expired(deadline):
                    drain_outbox()
                try:
                    outcome = self._process_runner(source.name, tuple(rows))
                except Exception as exc:
                    failures.append(f"process:{source.name}:{type(exc).__name__}")
                    remaining -= len(rows)
                    continue
                processed += len(rows)
                remaining -= len(rows)
                try:
                    self._queue_summary_progress(rows)
                except Exception as exc:
                    failures.append(f"notify:{source.name}:summary_progress:{type(exc).__name__}")
                if outcome.status == "partial":
                    failure_count = len(tuple(getattr(outcome, "failures", ()) or ()))
                    failures.append(f"process:{source.name}:partial:{failure_count}")
                elif outcome.status not in {"success", "busy"}:
                    failures.append(f"process:{source.name}:{outcome.status}")

        if "outbox" in stages and not self._expired(deadline):
            drain_outbox()
        elif "outbox" in stages:
            budget_exhausted = True

        status = "success" if not failures else "partial"
        return TickOutcome(
            status=status,
            scheduled=scheduled,
            downloaded=downloaded,
            processed=processed,
            notifications=tuple(notifications_by_key.values()),
            budget_exhausted=budget_exhausted,
            failures=tuple(failures),
        )

    def _extractor_version(self) -> str:
        return self.config.pipeline.extractor_version or "ocr-geometry-v2"

    def _notifications_configured(self) -> bool:
        return self.config.lark.notifications_enabled and bool(self.config.lark.target_chat_id)

    @staticmethod
    def _window_scope(source_window_id: int) -> str:
        return f"source-window:{int(source_window_id)}"

    @staticmethod
    def _source_window_ids(rows: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]]) -> tuple[int, ...]:
        values: set[int] = set()
        for row in rows:
            value = row.get("source_window_id")
            if value is None:
                continue
            try:
                window_id = int(value)
            except (TypeError, ValueError):
                continue
            if window_id > 0:
                values.add(window_id)
        return tuple(sorted(values))

    def _queue_download_complete(self, row: Mapping[str, Any], outcome: DownloadOutcome) -> None:
        """Queue one exact non-empty download count for a scheduled window."""

        if not self._notifications_configured():
            return
        entries = tuple(getattr(outcome, "downloaded_entries", ()) or ())
        if not entries:
            return
        try:
            window_id = int(row["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkerError("download notification requires a durable source window id") from exc
        scope = self._window_scope(window_id)
        with PipelineState.open(self.config.runtime.database) as state:
            state.migrate()
            enqueue_pipeline_status_notification(
                state,
                event=DOWNLOAD_COMPLETE_EVENT,
                identity=scope,
                chat_id=self.config.lark.target_chat_id,
                markdown=render_download_complete_notice(source=outcome.source, count=len(entries)),
                scope_key=scope,
            )

    def _queue_download_blocked(self, row: Mapping[str, Any], outcome: DownloadOutcome) -> None:
        """Queue one reason-specific alert while retaining the source window."""

        if not self._notifications_configured():
            return
        reason_code = str(getattr(outcome, "reason_code", "") or "").strip()
        if not reason_code:
            raise WorkerError("download blocked notification requires a reason code")
        try:
            window_id = int(row["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkerError("download blocked notification requires a durable source window id") from exc
        scope = self._window_scope(window_id)
        with PipelineState.open(self.config.runtime.database) as state:
            state.migrate()
            enqueue_pipeline_status_notification(
                state,
                event=DOWNLOAD_BLOCKED_EVENT,
                identity=f"{scope}:{reason_code}",
                chat_id=self.config.lark.target_chat_id,
                markdown=render_download_blocked_notice(source=outcome.source, reason_code=reason_code),
                scope_key=scope,
            )

    def _queue_summary_starts(self, rows: list[Mapping[str, Any]]) -> bool:
        """Queue at most one start status for each durable source window."""

        if not self._notifications_configured():
            return False
        created = False
        with PipelineState.open(self.config.runtime.database) as state:
            state.migrate()
            for window_id in self._source_window_ids(rows):
                progress = state.source_window_progress(window_id)
                if progress is None or int(progress["total"]) < 1:
                    continue
                scope = self._window_scope(window_id)
                record = enqueue_pipeline_status_notification(
                    state,
                    event=SUMMARY_STARTED_EVENT,
                    identity=scope,
                    chat_id=self.config.lark.target_chat_id,
                    markdown=render_summary_started_notice(
                        source=str(progress["source"]),
                        total=int(progress["total"]),
                    ),
                    scope_key=scope,
                )
                created = record.created or created
        return created

    def _queue_summary_progress(self, rows: list[Mapping[str, Any]]) -> None:
        """Queue only the highest newly reached milestone or terminal success."""

        if not self._notifications_configured():
            return
        with PipelineState.open(self.config.runtime.database) as state:
            state.migrate()
            for window_id in self._source_window_ids(rows):
                progress = state.source_window_progress(window_id)
                if progress is None:
                    continue
                total = int(progress["total"])
                summarized = int(progress["summarized"])
                published = int(progress["published"])
                if total < 1:
                    continue
                scope = self._window_scope(window_id)
                if summarized == total and published == total:
                    enqueue_pipeline_status_notification(
                        state,
                        event=BATCH_COMPLETE_EVENT,
                        identity=scope,
                        chat_id=self.config.lark.target_chat_id,
                        markdown=render_batch_complete_notice(
                            source=str(progress["source"]),
                            total=total,
                            summarized=summarized,
                            published=published,
                        ),
                        scope_key=scope,
                        terminal=True,
                    )
                    continue
                percentage = ((summarized + published) * 100) // (2 * total)
                reached = [milestone for milestone in (25, 50, 75) if percentage >= milestone]
                if not reached:
                    continue
                milestone = reached[-1]
                enqueue_pipeline_status_notification(
                    state,
                    event=SUMMARY_PROGRESS_EVENT,
                    identity=f"{scope}:milestone:{milestone}",
                    chat_id=self.config.lark.target_chat_id,
                    markdown=render_summary_progress_notice(
                        source=str(progress["source"]),
                        total=total,
                        summarized=summarized,
                        published=published,
                        milestone=milestone,
                    ),
                    scope_key=scope,
                )

    def _download_request(self, row: Mapping[str, Any]) -> DownloadRequest:
        source_name = str(row.get("source") or "").strip()
        source = self.config.sources.get(source_name)
        if source is None:
            raise WorkerError(f"scheduled source is absent from config: {source_name!r}")
        if source.job_config_path is None or source.keyword_path is None or source.state_path is None:
            raise WorkerError(f"sources.{source_name} needs job_config, keyword_file, and state_path for scheduled download")
        if not source.cdp_endpoint:
            raise WorkerError(f"sources.{source_name}.cdp_endpoint is required for scheduled download")
        cft_options = self._cft_options(source)
        return DownloadRequest(
            source=source_name,
            runtime_root=self.config.runtime.root,
            database=self.config.runtime.database,
            job_config_path=source.job_config_path,
            keyword_path=source.keyword_path,
            legacy_state_path=source.state_path,
            cdp_endpoint=source.cdp_endpoint,
            window_start=from_iso(str(row["window_start_iso"])),
            window_end=from_iso(str(row["window_end_iso"])),
            cft_launch_options=cft_options,
            workflow_version=source.workflow_version,
            extractor_version=self._extractor_version(),
            run_id="",
        )

    def _run_download_window(self, row: Mapping[str, Any]) -> DownloadOutcome | None:
        """Run only the uncovered suffix of a scheduled catch-up window.

        A transient browser outage can leave an older scheduled window while a
        later tick durably records another coalesced window.  Once the older
        one succeeds, the later row may already be fully covered.  Never scan
        that range again merely because the schedule rows overlap.
        """

        request = self._download_request(row)
        with PipelineState.open(self.config.runtime.database) as state:
            state.migrate()
            checkpoint = state.latest_source_checkpoint(request.source)
            if checkpoint is not None and checkpoint >= request.window_end:
                state.register_source_window(
                    request.source,
                    request.window_start,
                    request.window_end,
                    status="succeeded",
                    checkpoint_eligible=True,
                )
                return None
        if checkpoint is not None and checkpoint > request.window_start:
            request = replace(request, window_start=checkpoint)
        outcome = self._download_runner(request)
        if bool(getattr(outcome, "checkpoint_eligible", False)):
            # The runner persisted its effective suffix.  Close the original
            # scheduler row too, preserving its historical scheduled bounds.
            with PipelineState.open(self.config.runtime.database) as state:
                state.migrate()
                state.register_source_window(
                    str(row["source"]),
                    from_iso(str(row["window_start_iso"])),
                    from_iso(str(row["window_end_iso"])),
                    status="succeeded",
                    checkpoint_eligible=True,
                )
        return outcome

    @staticmethod
    def _cft_options(source: SourceConfig) -> CftLaunchOptions | None:
        executable = source.cft_executable_path
        profile = source.cft_user_data_dir
        if (executable is None) != (profile is None):
            raise WorkerError(f"sources.{source.name} must set both cft_executable and cft_user_data_dir")
        if executable is None:
            return None
        return CftLaunchOptions(
            executable_path=executable,
            user_data_dir=profile,
            start_url=source.cft_start_url,
            headless=source.cft_headless,
            background=source.cft_background,
            window_size=source.cft_window_size,
        )

    def _run_process(self, source: str, rows: tuple[Mapping[str, Any], ...]) -> ProcessOutcome:
        """Feed existing source identities into the direct processor exactly once."""

        files: list[dict[str, Any]] = []
        for row in rows:
            path = Path(str(row.get("canonical_path") or row.get("source_path") or "")).expanduser()
            files.append(
                {
                    "path": str(path),
                    "filename": str(row.get("filename") or path.name),
                    "pdf_sha256": str(row.get("pdf_sha256") or ""),
                    "source": source,
                    "source_file_id": str(row.get("source_file_id") or ""),
                    "source_window_id": row.get("source_window_id"),
                    "batch_id": "unified-tick",
                }
            )
        safe_source = "".join(character if character.isalnum() or character in "._-" else "-" for character in source)
        batch_path = self.config.runtime.root / "work" / "tick-batches" / f"{safe_source}.json"
        _atomic_json(
            batch_path,
            {
                "generated_at": self.clock().isoformat(),
                "root": "",
                "new_pdf_count": len(files),
                "files": files,
            },
        )
        base = ProcessConfig.from_pipeline_config(self.config)
        process_config = replace(base, source=source, batch_path=batch_path)
        return DigestProcessor(process_config, clock=self.clock).run(
            ProcessRequest(batch_file=batch_path, defer_notification_drain=True), acquire_lock=False
        )

    def _drain_outbox(self, max_items: int) -> tuple[NotificationDelivery, ...]:
        if not self.config.lark.notifications_enabled:
            return ()
        lark_config = LarkCliConfig(
            command=self.config.lark.command,
            config_dir=self.config.lark.config_dir,
            timeout_seconds=self.config.lark.timeout_seconds,
            parent_position=self.config.lark.parent_position,
        )
        with PipelineState.open(self.config.runtime.database) as state:
            state.migrate()
            deliveries = NotificationDrainer(state, LarkNotifier(lark_config), clock=self.clock).drain(max_items=max_items)
            # Compatibility output is an observer, never a second outbox.
            audit_path = self.config.runtime.root / "notification_messages.jsonl"
            export_notification_audit(state, audit_path)
            return deliveries


__all__ = ["PipelineWorker", "TickOutcome", "WorkerError"]
