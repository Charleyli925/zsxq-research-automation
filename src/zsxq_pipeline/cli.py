"""Command line interface for the state core and direct-Codex digest worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
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
from .worker import PipelineWorker, WorkerError


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
    doctor.add_argument("--state-only", action="store_true", help="Only inspect durable SQLite state.")

    tick = top.add_parser("tick", help="Discover due windows and run bounded unified pipeline work.")
    tick.add_argument("--config", required=True, help="Validated pipeline TOML configuration.")
    tick.add_argument("--budget-seconds", type=int, help="Override the configured one-shot wall-clock budget.")

    run_stage = top.add_parser("run-stage", help="Run one existing unified worker stage under the shared runtime lock.")
    run_stage.add_argument("--config", required=True, help="Validated pipeline TOML configuration.")
    run_stage.add_argument("--stage", required=True, choices=("download", "process", "outbox", "all"))
    run_stage.add_argument("--budget-seconds", type=int, help="Override the configured one-shot wall-clock budget.")

    outbox = top.add_parser("outbox", help="Operate the durable notification outbox.")
    outbox_sub = outbox.add_subparsers(dest="outbox_command", required=True)
    outbox_drain = outbox_sub.add_parser("drain", help="Deliver due notifications without scanning or downloading.")
    outbox_drain.add_argument("--config", required=True, help="Validated pipeline TOML configuration.")
    outbox_drain.add_argument("--budget-seconds", type=int, help="Override the configured one-shot wall-clock budget.")

    retry = top.add_parser("retry", help="Create and explicitly apply verified terminal-stage recovery plans.")
    retry_sub = retry.add_subparsers(dest="retry_command", required=True)
    retry_plan = retry_sub.add_parser("plan", help="Preview exact terminal failures matching one workflow/error code.")
    _state_locator(retry_plan)
    retry_plan.add_argument("--stage", required=True, choices=("download", "text_extract", "summary", "publish", "notify"))
    retry_plan.add_argument("--workflow-version", required=True)
    retry_plan.add_argument("--error-code", required=True)
    retry_plan.add_argument("--output", help="Optional JSON file for a reviewed immutable retry plan.")
    retry_apply = retry_sub.add_parser("apply", help="Verify then requeue exactly the candidates named by a retry plan.")
    _state_locator(retry_apply)
    retry_apply.add_argument("--plan", required=True, help="JSON created by `retry plan`.")
    retry_apply.add_argument("--expected-count", required=True, type=int)
    retry_apply.add_argument("--apply", action="store_true", help="Required before any terminal row may be requeued.")

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


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """Write operator-reviewed plan output without a partially visible file."""

    destination = Path(path).expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_retry_hash(payload: dict[str, Any]) -> str:
    value = dict(payload)
    value.pop("plan_hash", None)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _build_retry_plan(args: argparse.Namespace) -> dict[str, Any]:
    database = _database_from_args(args)
    with PipelineState.open(database) as state:
        state.migrate()
        candidates = state.plan_stage_retry(
            stage=args.stage,
            workflow_version=args.workflow_version,
            error_code=args.error_code,
        )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "database": str(database),
        "stage": str(args.stage),
        "workflow_version": str(args.workflow_version),
        "error_code": str(args.error_code),
        "expected_count": len(candidates),
        "candidates": candidates,
    }
    payload["plan_hash"] = _canonical_retry_hash(payload)
    return payload


def _load_retry_plan(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve(strict=True)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("retry plan is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ConfigError("retry plan has an unsupported schema")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise ConfigError("retry plan candidates must be an array")
    expected_hash = str(payload.get("plan_hash") or "").strip().lower()
    if expected_hash != _canonical_retry_hash(payload):
        raise ConfigError("retry plan hash does not match its immutable contents")
    return payload


def _command_available(value: str) -> bool:
    command = str(value).strip()
    if not command:
        return False
    candidate = Path(command).expanduser()
    if candidate.is_absolute() or "/" in command:
        return candidate.is_file() and os.access(candidate, os.X_OK)
    return shutil.which(command) is not None


def _doctor_from_args(args: argparse.Namespace) -> dict[str, Any]:
    database = _database_from_args(args)
    report = doctor_state_only(database)
    if bool(args.state_only) or not getattr(args, "config", None):
        return report
    config = load_pipeline_config(args.config)
    source_checks: dict[str, dict[str, bool]] = {}
    migration_debt: list[str] = []
    for source in config.sources.values():
        values = (source.job_config_path, source.keyword_path, source.state_path, source.cft_user_data_dir)
        if any(value is not None and ".openclaw" in str(value) for value in values):
            migration_debt.append(f"source:{source.name}:legacy_profile_path")
        source_checks[source.name] = {
            "scheduled": bool(source.schedule_times),
            "download_configured": bool(
                source.job_config_path is not None
                and source.job_config_path.is_file()
                and source.keyword_path is not None
                and source.keyword_path.is_file()
                and source.state_path is not None
                and bool(source.cdp_endpoint)
            ),
            "cft_pair_valid": (source.cft_executable_path is None) == (source.cft_user_data_dir is None),
            "cft_executable_available": source.cft_executable_path is None
            or (source.cft_executable_path.is_file() and os.access(source.cft_executable_path, os.X_OK)),
        }
    checks = {
        "runtime_root_exists": config.runtime.root.is_dir(),
        "codex_command_available": _command_available(config.codex.command),
        "lark_command_available": _command_available(config.lark.command),
        "sources": source_checks,
    }
    if config.lark.config_dir is not None and ".openclaw" in str(config.lark.config_dir):
        migration_debt.append("lark:legacy_profile_path")
    configured_sources_ok = all(
        values["download_configured"] and values["cft_pair_valid"] and values["cft_executable_available"]
        for values in source_checks.values()
        if values["scheduled"]
    )
    report["checks"] = checks
    report["migration_debt"] = sorted(set(migration_debt))
    report["ok"] = bool(report["ok"] and checks["runtime_root_exists"] and checks["codex_command_available"] and checks["lark_command_available"] and configured_sources_ok)
    return report


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
            report = _doctor_from_args(args)
            _emit(report)
            return 0 if bool(report["ok"]) else 1
        if args.command == "tick":
            outcome = PipelineWorker(load_pipeline_config(args.config)).tick(budget_seconds=args.budget_seconds)
            _emit(outcome.to_dict())
            return 0 if outcome.status in {"success", "busy"} else 1
        if args.command == "run-stage":
            outcome = PipelineWorker(load_pipeline_config(args.config)).run_stage(
                args.stage, budget_seconds=args.budget_seconds
            )
            _emit(outcome.to_dict())
            return 0 if outcome.status in {"success", "busy"} else 1
        if args.command == "outbox" and args.outbox_command == "drain":
            outcome = PipelineWorker(load_pipeline_config(args.config)).run_stage(
                "outbox", budget_seconds=args.budget_seconds
            )
            _emit(outcome.to_dict())
            return 0 if outcome.status in {"success", "busy"} else 1
        if args.command == "retry" and args.retry_command == "plan":
            plan = _build_retry_plan(args)
            if args.output:
                destination = Path(args.output).expanduser().resolve(strict=False)
                _atomic_json(destination, plan)
                _emit(
                    {
                        "planned": True,
                        "plan_path": str(destination),
                        "plan_hash": plan["plan_hash"],
                        "expected_count": plan["expected_count"],
                    }
                )
            else:
                _emit(plan)
            return 0
        if args.command == "retry" and args.retry_command == "apply":
            plan = _load_retry_plan(args.plan)
            database = _database_from_args(args)
            if str(database) != str(plan.get("database", "")):
                raise ConfigError("retry plan belongs to a different database")
            if int(args.expected_count) != int(plan.get("expected_count", -1)):
                raise ConfigError("--expected-count does not match the reviewed retry plan")
            if not args.apply:
                _emit(
                    {
                        "applied": False,
                        "reason": "pass --apply to requeue reviewed terminal stages",
                        "plan_hash": plan["plan_hash"],
                        "expected_count": plan["expected_count"],
                    }
                )
                return 0
            with PipelineState.open(database) as state:
                state.migrate()
                requeued = state.apply_stage_retry_plan(
                    plan["candidates"],
                    plan_hash=str(plan["plan_hash"]),
                    expected_count=int(args.expected_count),
                )
            _emit({"applied": True, "requeued": requeued, "plan_hash": plan["plan_hash"]})
            return 0
        if args.command == "process":
            outcome = _process_from_args(args)
            _emit(outcome.to_dict())
            return 0 if outcome.status in {"success", "busy"} else 1
        if args.command == "download":
            outcome = _download_from_args(args)
            _emit(outcome.to_dict())
            return 0 if outcome.status == "success" else 1
    except (
        ConfigError,
        DownloadError,
        LegacyImportError,
        ProcessError,
        SchemaVersionError,
        StateError,
        WorkerError,
        ValueError,
    ) as exc:
        print(f"zsxq-pipeline: {exc}", file=sys.stderr)
        return 2
    parser.error("unsupported command")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
