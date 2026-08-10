"""Command line interface for the state core and direct-Codex digest worker."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ConfigError, load_pipeline_config, resolve_database
from .browser import CftLaunchOptions
from .download import DownloadError, DownloadPipeline, DownloadRequest, _parse_datetime, _read_legacy_checkpoint
from .legacy_import import (
    LegacyImportError,
    apply_import_plan,
    build_import_plan,
    load_import_plan,
    write_import_plan,
)
from .process import DigestProcessor, ProcessConfig, ProcessError, ProcessOutcome, ProcessRequest
from .schema import SchemaVersionError
from .state import PipelineState, StateError
from .status import doctor_state_only, read_status


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _state_locator(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--config", help="Validated pipeline TOML configuration.")
    group.add_argument("--database", help="Pipeline SQLite path (for local diagnostics and tests).")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zsxq-pipeline", description="ZSXQ pipeline state core and direct Codex/Lark worker.")
    top = parser.add_subparsers(dest="command", required=True)

    db = top.add_parser("db", help="Manage the pipeline SQLite schema.")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    migrate = db_sub.add_parser("migrate", help="Apply forward-only schema migrations.")
    _state_locator(migrate)

    legacy = top.add_parser("legacy", help="Preview or explicitly import legacy state.")
    legacy_sub = legacy.add_subparsers(dest="legacy_command", required=True)
    plan = legacy_sub.add_parser("plan", help="Read legacy state and produce an immutable import plan.")
    plan_sources = plan.add_mutually_exclusive_group(required=True)
    plan_sources.add_argument("--legacy-root", help="Absolute legacy runtime root to read.")
    plan_sources.add_argument("--config", help="Pipeline TOML whose [legacy].root supplies the legacy root.")
    plan.add_argument("--output", help="Optional JSON plan path. Without it, plan is printed to stdout.")
    apply = legacy_sub.add_parser("apply", help="Verify a plan then idempotently write the new pipeline database.")
    apply.add_argument("--plan", required=True, help="Plan created by `legacy plan`.")
    _state_locator(apply)
    apply.add_argument("--apply", action="store_true", help="Required before the destination database may be written.")

    status = top.add_parser("status", help="Derive health from durable state.")
    _state_locator(status)
    status.add_argument("--json", action="store_true", required=True, help="Emit the stable JSON status schema.")

    doctor = top.add_parser("doctor", help="Run bounded diagnostics.")
    _state_locator(doctor)
    doctor.add_argument("--state-only", action="store_true", required=True, help="Do not probe network, browser, model, or Feishu.")

    process = top.add_parser("process", help="Extract, summarize with direct Codex, publish through lark-cli, and export compatibility status.")
    process_source = process.add_mutually_exclusive_group(required=True)
    process_source.add_argument("--config", help="Validated direct pipeline TOML configuration.")
    process_source.add_argument("--runtime-root", help="Task runtime root; direct settings are read from its sourced config.env environment.")
    process.add_argument("--file", action="append", default=[], help="Absolute PDF path; may be repeated.")
    process.add_argument("--folder", action="append", default=[], help="Folder of PDFs to process; may be repeated.")
    process.add_argument("--batch-file", help="Existing batch manifest to copy into the runtime before processing.")
    process.add_argument("--dry-run", action="store_true", help="Do not publish to Lark or send a notification.")
    process.add_argument("--summary-only", action="store_true", help="Stop after local direct-Codex summary artifacts.")
    process.add_argument("--no-notify", action="store_true", help="Publish documents but leave the notification outbox undrained.")
    process.add_argument("--preflight-only", action="store_true", help="Run bounded extractor/Codex/Lark capability checks only.")
    process.add_argument("--include-existing", action="store_true", help="Include existing PDFs on the first scanner baseline.")

    download = top.add_parser("download", help="Scan, download, archive, and reconcile one immutable ZSXQ source window.")
    download.add_argument("--source", required=True, help="Logical source name from pipeline config or runtime settings.")
    source = download.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", help="Validated pipeline TOML configuration.")
    source.add_argument("--runtime-root", help="Explicit runtime root for compatibility-task execution.")
    download.add_argument("--database", help="SQLite state path when --runtime-root is used.")
    download.add_argument("--job-config", help="Absolute source job JSON when --runtime-root is used.")
    download.add_argument("--keyword-file", help="Absolute keyword JSON when --runtime-root is used.")
    download.add_argument("--legacy-state", help="Existing checkpoint mirror read by the archive finalizer only.")
    download.add_argument("--cdp-endpoint", help="Dedicated Chrome for Testing DevTools endpoint.")
    download.add_argument("--cft-executable", help="Absolute Chrome for Testing executable; enables bounded CDP startup.")
    download.add_argument("--cft-user-data-dir", help="Absolute dedicated Chrome for Testing profile directory.")
    download.add_argument("--cft-start-url", default="", help="Optional page retained in the dedicated browser session.")
    download.add_argument("--cft-headless", choices=("true", "false"))
    download.add_argument("--cft-window-size", default="")
    download.add_argument("--window-start", help="Explicit ISO-8601 window start; omit with --window-end to resume checkpoint.")
    download.add_argument("--window-end", help="Explicit ISO-8601 window end; omit with --window-start to use now.")
    download.add_argument("--workflow-version", default="", help="Override the durable download stage version for compatibility runs.")
    download.add_argument("--timeout-seconds", type=int, default=30, help="Bound each browser action and CDP connection.")
    download.add_argument("--navigation-attempts", type=int, default=3)
    download.add_argument("--plan-only", action="store_true", help="Write and report an immutable plan without downloading or changing state.")
    download.add_argument("--dry-run", action="store_true", help="Run the finalizer in dry-run mode after plan-bound downloads.")
    download.add_argument("--result-path", help="Write the canonical compatibility result JSON atomically.")
    download.add_argument("--run-id", default="", help="Optional UUID supplied by a scheduler wrapper.")
    return parser


def _database_from_args(args: argparse.Namespace) -> Path:
    return resolve_database(config_path=getattr(args, "config", None), database=getattr(args, "database", None))


def _legacy_root_from_args(args: argparse.Namespace) -> Path:
    if args.legacy_root:
        root = Path(args.legacy_root).expanduser()
        if not root.is_absolute():
            raise ConfigError("--legacy-root must be absolute")
        return root.resolve(strict=True)
    config = load_pipeline_config(args.config)
    if config.legacy.root is None:
        raise ConfigError("[legacy].root is required when `legacy plan --config` is used")
    return config.legacy.root


def _process_from_args(args: argparse.Namespace) -> ProcessOutcome:
    if args.config:
        config = ProcessConfig.from_pipeline_config(load_pipeline_config(args.config))
    else:
        runtime_root = Path(args.runtime_root).expanduser()
        if not runtime_root.is_absolute():
            raise ConfigError("--runtime-root must be absolute")
        config = ProcessConfig.from_environment(runtime_root)
    request = ProcessRequest(
        files=tuple(Path(value).expanduser() for value in args.file),
        folders=tuple(Path(value).expanduser() for value in args.folder),
        batch_file=Path(args.batch_file).expanduser() if args.batch_file else None,
        dry_run=bool(args.dry_run),
        summary_only=bool(args.summary_only),
        no_notify=bool(args.no_notify),
        preflight_only=bool(args.preflight_only),
        include_existing=bool(args.include_existing),
    )
    return DigestProcessor(config).run(request)


def _absolute_path(value: str | None, *, field: str) -> Path:
    path = Path(str(value or "").strip()).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{field} must be an absolute path")
    return path.resolve(strict=False)


def _download_from_args(args: argparse.Namespace):
    source = str(args.source).strip()
    if args.config:
        config = load_pipeline_config(args.config)
        source_config = config.sources.get(source)
        if source_config is None:
            raise ConfigError(f"unknown configured source: {source}")
        if source_config.job_config_path is None or source_config.keyword_path is None or source_config.state_path is None:
            raise ConfigError(f"sources.{source} needs job_config, keyword_file, and state_path for download")
        if not source_config.cdp_endpoint:
            raise ConfigError(f"sources.{source}.cdp_endpoint is required for download")
        runtime_root = config.runtime.root
        database = config.runtime.database
        job_config = source_config.job_config_path
        keyword_path = source_config.keyword_path
        legacy_state = source_config.state_path
        cdp_endpoint = source_config.cdp_endpoint
        workflow_version = str(args.workflow_version or source_config.workflow_version).strip()
        extractor_version = config.pipeline.extractor_version
        configured_cft_executable = source_config.cft_executable_path
        configured_cft_profile = source_config.cft_user_data_dir
        configured_cft_start_url = source_config.cft_start_url
        configured_cft_headless = source_config.cft_headless
        configured_cft_window_size = source_config.cft_window_size
    else:
        runtime_root = _absolute_path(args.runtime_root, field="--runtime-root")
        database = _absolute_path(args.database, field="--database") if args.database else runtime_root / "state" / "pipeline.sqlite3"
        job_config = _absolute_path(args.job_config, field="--job-config")
        keyword_path = _absolute_path(args.keyword_file, field="--keyword-file")
        legacy_state = _absolute_path(args.legacy_state, field="--legacy-state")
        cdp_endpoint = str(args.cdp_endpoint or "").strip()
        if not cdp_endpoint:
            raise ConfigError("--cdp-endpoint is required with --runtime-root")
        workflow_version = str(args.workflow_version or "download:v1").strip()
        extractor_version = "extract:v1"
        configured_cft_executable = None
        configured_cft_profile = None
        configured_cft_start_url = ""
        configured_cft_headless = True
        configured_cft_window_size = "1440,1200"

    if bool(args.window_start) != bool(args.window_end):
        raise ConfigError("--window-start and --window-end must be supplied together")
    if args.window_start:
        window_start = _parse_datetime(args.window_start)
        window_end = _parse_datetime(args.window_end)
    else:
        with PipelineState.open(database) as state:
            state.migrate()
            checkpoint = state.latest_source_checkpoint(source)
        window_start = checkpoint or _read_legacy_checkpoint(legacy_state)
        if window_start is None:
            raise ConfigError("no durable checkpoint exists; pass --window-start and --window-end explicitly")
        window_end = datetime.now().astimezone()

    cft_executable = _absolute_path(args.cft_executable, field="--cft-executable") if str(args.cft_executable or "").strip() else configured_cft_executable
    cft_profile = _absolute_path(args.cft_user_data_dir, field="--cft-user-data-dir") if str(args.cft_user_data_dir or "").strip() else configured_cft_profile
    has_cft_executable = cft_executable is not None
    has_cft_profile = cft_profile is not None
    if has_cft_executable != has_cft_profile:
        raise ConfigError("--cft-executable and --cft-user-data-dir must be supplied together")
    cft_launch_options = None
    if has_cft_executable:
        cft_launch_options = CftLaunchOptions(
            executable_path=cft_executable,
            user_data_dir=cft_profile,
            start_url=str(args.cft_start_url or configured_cft_start_url).strip(),
            headless=(str(args.cft_headless).strip().lower() == "true") if args.cft_headless is not None else configured_cft_headless,
            window_size=str(args.cft_window_size or configured_cft_window_size).strip(),
        )

    request = DownloadRequest(
        source=source,
        runtime_root=runtime_root,
        database=database,
        job_config_path=job_config,
        keyword_path=keyword_path,
        legacy_state_path=legacy_state,
        cdp_endpoint=cdp_endpoint,
        window_start=window_start,
        window_end=window_end,
        cft_launch_options=cft_launch_options,
        workflow_version=workflow_version,
        extractor_version=extractor_version,
        timeout_ms=max(1_000, int(args.timeout_seconds) * 1_000),
        navigation_attempts=max(1, int(args.navigation_attempts)),
        plan_only=bool(args.plan_only),
        dry_run=bool(args.dry_run),
        result_path=_absolute_path(args.result_path, field="--result-path") if args.result_path else None,
        run_id=str(args.run_id or "").strip(),
    )
    return DownloadPipeline().run(request)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "db" and args.db_command == "migrate":
            database = _database_from_args(args)
            with PipelineState.open(database) as state:
                version = state.migrate()
            _emit({"database": str(database), "schema_version": version, "migrated": True})
            return 0
        if args.command == "legacy" and args.legacy_command == "plan":
            plan = build_import_plan(_legacy_root_from_args(args))
            if args.output:
                write_import_plan(plan, args.output)
                _emit({"plan_path": str(Path(args.output).expanduser().resolve(strict=False)), "summary": plan.to_dict()["summary"]})
            else:
                _emit(plan.to_dict())
            return 0
        if args.command == "legacy" and args.legacy_command == "apply":
            plan = load_import_plan(args.plan)
            if not args.apply:
                _emit({"applied": False, "reason": "pass --apply to write the destination pipeline database", "summary": plan.to_dict()["summary"]})
                return 0
            database = _database_from_args(args)
            _emit({"applied": True, "database": str(database), "counts": apply_import_plan(plan, database)})
            return 0
        if args.command == "status":
            _emit(read_status(_database_from_args(args)))
            return 0
        if args.command == "doctor":
            _emit(doctor_state_only(_database_from_args(args)))
            return 0
        if args.command == "process":
            outcome = _process_from_args(args)
            _emit(outcome.to_dict())
            return 0 if outcome.status in {"success", "busy"} else 1
        if args.command == "download":
            outcome = _download_from_args(args)
            _emit(outcome.to_dict())
            return 0 if outcome.status == "success" else 1
    except (ConfigError, DownloadError, LegacyImportError, ProcessError, SchemaVersionError, StateError, ValueError) as exc:
        print(f"zsxq-pipeline: {exc}", file=sys.stderr)
        return 2
    parser.error("unsupported command")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
