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
from pathlib import Path
from typing import Any

from .config import ConfigError, load_pipeline_config, resolve_database
from .legacy_import import (
    LegacyImportError,
    apply_import_plan,
    build_import_plan,
    load_import_plan,
    write_import_plan,
)
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
        "sidecars": {
            "research_library_available": config.pipeline.research_library_root is None
            or (
                config.pipeline.research_library_root.is_dir()
                and os.access(config.pipeline.research_library_root, os.W_OK)
            ),
            "research_library_database_available": config.pipeline.research_library_database is None
            or (
                config.pipeline.research_library_database.parent.is_dir()
                and os.access(config.pipeline.research_library_database.parent, os.W_OK)
            ),
            "obsidian_vault_available": config.pipeline.obsidian_vault_root is None
            or (
                config.pipeline.obsidian_vault_root.is_dir()
                and os.access(config.pipeline.obsidian_vault_root, os.W_OK)
            ),
        },
    }
    if config.lark.config_dir is not None and ".openclaw" in str(config.lark.config_dir):
        migration_debt.append("lark:legacy_profile_path")
    configured_sources_ok = all(
        values["download_configured"] and values["cft_pair_valid"] and values["cft_executable_available"]
        for values in source_checks.values()
        if values["scheduled"]
    )
    sidecars_ok = all(checks["sidecars"].values())
    report["checks"] = checks
    report["migration_debt"] = sorted(set(migration_debt))
    report["ok"] = bool(
        report["ok"]
        and checks["runtime_root_exists"]
        and checks["codex_command_available"]
        and checks["lark_command_available"]
        and configured_sources_ok
        and sidecars_ok
    )
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
    except (
        ConfigError,
        LegacyImportError,
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
