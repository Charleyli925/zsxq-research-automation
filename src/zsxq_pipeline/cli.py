"""No-side-effect command line interface for the pipeline state core."""

from __future__ import annotations

import argparse
import json
import sys
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


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _state_locator(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--config", help="Validated pipeline TOML configuration.")
    group.add_argument("--database", help="Pipeline SQLite path (for local diagnostics and tests).")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zsxq-pipeline", description="ZSXQ pipeline state core; no worker integrations yet.")
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
    except (ConfigError, LegacyImportError, SchemaVersionError, StateError, ValueError) as exc:
        print(f"zsxq-pipeline: {exc}", file=sys.stderr)
        return 2
    parser.error("unsupported command")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
